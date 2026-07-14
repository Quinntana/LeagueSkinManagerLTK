// SPDX-License-Identifier: MIT OR Apache-2.0

use crate::error::EngineError;
use crate::path_output::conventional_path;
use serde::{Deserialize, Serialize};
use std::fs::File;
use std::io::Read;
use std::path::{Path, PathBuf};

const HOST_FILE_NAME: &str = "ltk_patcher_host.exe";
const HOOK_FILE_NAME: &str = "ltk_patcher_dll.dll";
const MAX_HOST_EVENT_LINES: usize = 1024;
const MAX_HOST_EVENT_LINE_BYTES: usize = 64 * 1024;

#[derive(Debug, Clone, Copy, Default, Deserialize, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum HostLogLevel {
    Error,
    #[default]
    Info,
    Debug,
}

impl HostLogLevel {
    const fn protocol_value(self) -> u32 {
        match self {
            Self::Error => 0,
            Self::Info => 0x10,
            Self::Debug => 0x20,
        }
    }
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ProviderSmokeParams {
    pub installation_dir: String,
    pub overlay_prefix: String,
    #[serde(default)]
    pub log_level: HostLogLevel,
    #[serde(default)]
    pub event_lines: Vec<String>,
}

#[derive(Debug, Serialize)]
pub struct ProviderSmokeResult {
    pub provider: &'static str,
    pub available: bool,
    pub configuration_only: bool,
    pub process_started: bool,
    pub anti_hack_enforced: bool,
    pub host_executable: String,
    pub hook_library: String,
    pub config_lines: Vec<String>,
    pub parsed_events: Vec<ParsedHostLine>,
}

#[derive(Debug, Serialize)]
pub struct ParsedHostLine {
    pub raw: String,
    pub parsed: bool,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub event: Option<HostEvent>,
}

#[derive(Debug, Serialize, PartialEq, Eq)]
#[serde(tag = "kind", rename_all = "snake_case")]
pub enum HostEvent {
    Ok {
        timestamp: String,
        message: String,
    },
    Status {
        timestamp: String,
        state: HostState,
        message: String,
    },
    Error {
        timestamp: String,
        message: String,
    },
    DllLog {
        timestamp: String,
        pid: u64,
        tid: u64,
        level: String,
        message: String,
    },
}

#[derive(Debug, Serialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum HostState {
    Injecting,
    Injected,
    Waiting,
    Exited,
    Failed,
}

impl HostState {
    fn parse(value: &str) -> Option<Self> {
        match value {
            "injecting" => Some(Self::Injecting),
            "injected" => Some(Self::Injected),
            "waiting" => Some(Self::Waiting),
            "exited" => Some(Self::Exited),
            "failed" => Some(Self::Failed),
            _ => None,
        }
    }
}

/// Boundary for an externally supplied injection provider.
///
/// Deliberately limited to validation and configuration generation: this engine
/// never spawns the provider, injects a DLL, or acquires provider binaries.
pub trait InjectionProvider {
    fn smoke(
        &self,
        overlay_prefix: &Path,
        log_level: HostLogLevel,
        event_lines: Vec<String>,
    ) -> Result<ProviderSmokeResult, EngineError>;
}

pub struct InstalledLtkProvider {
    installation_dir: PathBuf,
}

impl InstalledLtkProvider {
    pub fn new(installation_dir: &str) -> Result<Self, EngineError> {
        if installation_dir.trim().is_empty() {
            return Err(EngineError::invalid_params(
                "installation_dir must not be empty",
            ));
        }
        let raw = PathBuf::from(installation_dir);
        let installation_dir = raw.canonicalize().map_err(|error| {
            EngineError::new(
                "provider_unavailable",
                format!(
                    "cannot resolve installed LTK Manager directory '{}': {error}",
                    raw.display()
                ),
            )
        })?;
        if !installation_dir.is_dir() {
            return Err(EngineError::new(
                "provider_unavailable",
                format!(
                    "LTK Manager installation path is not a directory: {}",
                    installation_dir.display()
                ),
            ));
        }
        Ok(Self { installation_dir })
    }

    fn resolve_provider_pair(&self) -> Result<(PathBuf, PathBuf), EngineError> {
        for base in [
            self.installation_dir.clone(),
            self.installation_dir.join("resources"),
        ] {
            let host_candidate = base.join(HOST_FILE_NAME);
            let hook_candidate = base.join(HOOK_FILE_NAME);
            if !host_candidate.is_file() || !hook_candidate.is_file() {
                continue;
            }

            let host = self.resolve_binary_at(&base, HOST_FILE_NAME)?;
            let hook = self.resolve_binary_at(&base, HOOK_FILE_NAME)?;
            if host.parent() != hook.parent() {
                return Err(EngineError::new(
                    "provider_unavailable",
                    "provider host and hook library must resolve from the same directory",
                ));
            }
            return Ok((host, hook));
        }
        Err(EngineError::new(
            "provider_unavailable",
            format!(
                "{HOST_FILE_NAME} and {HOOK_FILE_NAME} were not found together in installed LTK Manager directory '{}'",
                self.installation_dir.display()
            ),
        ))
    }

    fn resolve_binary_at(&self, base: &Path, name: &str) -> Result<PathBuf, EngineError> {
        let candidate = base.join(name);
        let canonical = candidate.canonicalize().map_err(|error| {
            EngineError::new(
                "provider_unavailable",
                format!(
                    "cannot resolve provider binary '{}': {error}",
                    candidate.display()
                ),
            )
        })?;
        if !canonical.starts_with(&self.installation_dir) {
            return Err(EngineError::new(
                "provider_unavailable",
                format!("provider binary escapes installation directory: {name}"),
            ));
        }
        verify_pe_header(&canonical)?;
        Ok(canonical)
    }
}

impl InjectionProvider for InstalledLtkProvider {
    fn smoke(
        &self,
        overlay_prefix: &Path,
        log_level: HostLogLevel,
        event_lines: Vec<String>,
    ) -> Result<ProviderSmokeResult, EngineError> {
        let (host, hook) = self.resolve_provider_pair()?;
        let overlay_prefix = canonical_overlay_prefix(overlay_prefix)?;
        let prefix = host_prefix(&overlay_prefix)?;
        if event_lines.len() > MAX_HOST_EVENT_LINES {
            return Err(EngineError::invalid_params(format!(
                "event_lines may contain at most {MAX_HOST_EVENT_LINES} lines"
            )));
        }
        let mut parsed_events = Vec::with_capacity(event_lines.len());
        for line in event_lines {
            if line.len() > MAX_HOST_EVENT_LINE_BYTES {
                return Err(EngineError::invalid_params(format!(
                    "a host event line exceeds {MAX_HOST_EVENT_LINE_BYTES} bytes"
                )));
            }
            let event = parse_host_event(&line);
            parsed_events.push(ParsedHostLine {
                raw: line,
                parsed: event.is_some(),
                event,
            });
        }

        Ok(ProviderSmokeResult {
            provider: "installed_ltk_manager",
            available: true,
            configuration_only: true,
            process_started: false,
            anti_hack_enforced: true,
            host_executable: conventional_path(&host),
            hook_library: conventional_path(&hook),
            config_lines: vec![
                format!("config loglevel {}", log_level.protocol_value()),
                // This value is fixed. No caller-controlled hook flags exist in
                // this API, and the anti-hack opt-out bit is never defined here.
                "config flags 0".to_owned(),
                format!("config prefix {prefix}"),
            ],
            parsed_events,
        })
    }
}

pub fn smoke_provider(params: ProviderSmokeParams) -> Result<ProviderSmokeResult, EngineError> {
    let provider = InstalledLtkProvider::new(&params.installation_dir)?;
    provider.smoke(
        Path::new(&params.overlay_prefix),
        params.log_level,
        params.event_lines,
    )
}

pub fn parse_host_event(line: &str) -> Option<HostEvent> {
    let line = line.trim_end_matches(['\r', '\n']);
    if line.is_empty() {
        return None;
    }
    let (keyword, rest) = split_first_token(line);
    match keyword {
        "ok" => {
            let (timestamp, message) = split_first_token(rest);
            (!timestamp.is_empty()).then(|| HostEvent::Ok {
                timestamp: timestamp.to_owned(),
                message: message.to_owned(),
            })
        }
        "status" => {
            let (timestamp, rest) = split_first_token(rest);
            let (state, message) = split_first_token(rest);
            Some(HostEvent::Status {
                timestamp: (!timestamp.is_empty()).then(|| timestamp.to_owned())?,
                state: HostState::parse(state)?,
                message: message.to_owned(),
            })
        }
        "error" => {
            let (timestamp, message) = split_first_token(rest);
            (!timestamp.is_empty()).then(|| HostEvent::Error {
                timestamp: timestamp.to_owned(),
                message: message.to_owned(),
            })
        }
        "dll" => {
            let (timestamp, rest) = split_first_token(rest);
            let (pid, rest) = split_first_token(rest);
            let (tid, rest) = split_first_token(rest);
            let (level, message) = split_first_token(rest);
            Some(HostEvent::DllLog {
                timestamp: (!timestamp.is_empty()).then(|| timestamp.to_owned())?,
                pid: pid.parse().ok()?,
                tid: tid.parse().ok()?,
                level: (!level.is_empty()).then(|| level.to_owned())?,
                message: message.to_owned(),
            })
        }
        _ => None,
    }
}

fn split_first_token(value: &str) -> (&str, &str) {
    let value = value.trim_start();
    match value.find([' ', '\t']) {
        Some(index) => (&value[..index], value[index + 1..].trim_start()),
        None => (value, ""),
    }
}

fn verify_pe_header(path: &Path) -> Result<(), EngineError> {
    let mut file = File::open(path).map_err(|error| {
        EngineError::new(
            "provider_unavailable",
            format!("cannot read provider binary '{}': {error}", path.display()),
        )
    })?;
    let mut header = [0_u8; 2];
    file.read_exact(&mut header).map_err(|error| {
        EngineError::new(
            "provider_unavailable",
            format!("provider binary '{}' is truncated: {error}", path.display()),
        )
    })?;
    if &header != b"MZ" {
        return Err(EngineError::new(
            "provider_unavailable",
            format!(
                "provider binary '{}' is not a Windows PE file",
                path.display()
            ),
        ));
    }
    Ok(())
}

fn canonical_overlay_prefix(path: &Path) -> Result<PathBuf, EngineError> {
    let canonical = path.canonicalize().map_err(|error| {
        EngineError::invalid_path(format!(
            "cannot resolve overlay_prefix '{}': {error}",
            path.display()
        ))
    })?;
    if !canonical.is_dir() {
        return Err(EngineError::invalid_path(format!(
            "overlay_prefix is not a directory: {}",
            canonical.display()
        )));
    }
    Ok(canonical)
}

fn host_prefix(path: &Path) -> Result<String, EngineError> {
    let mut value = conventional_path(path);
    if value.contains(['\r', '\n', '\0']) {
        return Err(EngineError::invalid_path(
            "overlay_prefix contains a character unsafe for the host line protocol",
        ));
    }
    if !value.ends_with(['/', '\\']) {
        value.push(std::path::MAIN_SEPARATOR);
    }
    Ok(value)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::fs;

    #[test]
    fn parses_all_known_host_events() {
        assert!(matches!(
            parse_host_event("ok 1.0 config prefix set"),
            Some(HostEvent::Ok { .. })
        ));
        assert!(matches!(
            parse_host_event("status 2.0 injecting scanning"),
            Some(HostEvent::Status {
                state: HostState::Injecting,
                ..
            })
        ));
        assert!(matches!(
            parse_host_event("error 3.0 bad command"),
            Some(HostEvent::Error { .. })
        ));
        assert_eq!(
            parse_host_event("dll 4.0 12 34 INFO redirected"),
            Some(HostEvent::DllLog {
                timestamp: "4.0".to_owned(),
                pid: 12,
                tid: 34,
                level: "INFO".to_owned(),
                message: "redirected".to_owned(),
            })
        );
        assert!(parse_host_event("other 1.0 ignored").is_none());
    }

    #[test]
    fn installed_provider_smoke_never_starts_or_opts_out() {
        let install = tempfile::tempdir().unwrap();
        let overlay = tempfile::tempdir().unwrap();
        fs::write(install.path().join(HOST_FILE_NAME), b"MZhost").unwrap();
        fs::write(install.path().join(HOOK_FILE_NAME), b"MZhook").unwrap();

        let provider = InstalledLtkProvider::new(install.path().to_str().unwrap()).unwrap();
        let result = provider
            .smoke(overlay.path(), HostLogLevel::Info, Vec::new())
            .unwrap();
        assert!(result.available);
        assert!(result.configuration_only);
        assert!(!result.process_started);
        assert!(result.anti_hack_enforced);
        assert_eq!(result.config_lines[1], "config flags 0");
        assert!(!result.host_executable.starts_with(r"\\?\"));
        assert!(!result.hook_library.starts_with(r"\\?\"));
        assert!(!result.config_lines[2].contains(r"\\?\"));
    }

    #[test]
    fn installed_provider_rejects_host_and_hook_from_different_directories() {
        let install = tempfile::tempdir().unwrap();
        let overlay = tempfile::tempdir().unwrap();
        let resources = install.path().join("resources");
        fs::create_dir(&resources).unwrap();
        fs::write(install.path().join(HOST_FILE_NAME), b"MZhost").unwrap();
        fs::write(resources.join(HOOK_FILE_NAME), b"MZhook").unwrap();

        let provider = InstalledLtkProvider::new(install.path().to_str().unwrap()).unwrap();
        let error = match provider.smoke(overlay.path(), HostLogLevel::Info, Vec::new()) {
            Ok(_) => panic!("provider binaries from different directories were accepted"),
            Err(error) => error,
        };
        assert_eq!(error.code(), "provider_unavailable");
        assert!(error.message().contains("not found together"));
    }
}
