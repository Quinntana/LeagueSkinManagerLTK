// SPDX-License-Identifier: MIT OR Apache-2.0

use crate::error::EngineError;
use crate::package::{
    PackageFormat, ensure_fantome_entry_count, ensure_package_file_size, package_format,
};
use crate::path_output::conventional_path;
use crate::protocol::Emitter;
use camino::Utf8PathBuf;
use ltk_modpkg::Modpkg;
use ltk_overlay::{
    EnabledMod, FantomeContent, ModContentProvider, ModpkgContent, OverlayBuilder, OverlayProgress,
    OverlayStage,
};
use serde::{Deserialize, Serialize};
use std::collections::HashSet;
use std::fs::File;
use std::io::Write;
use std::path::{Path, PathBuf};
use zip::ZipArchive;

const MAX_ENABLED_PACKAGES: usize = 1024;

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct BuildOverlayParams {
    pub game_dir: String,
    pub overlay_dir: String,
    pub state_dir: String,
    pub enabled_packages: Vec<String>,
}

#[derive(Debug, Serialize)]
pub struct BuildOverlayResult {
    pub overlay_root: String,
    pub enabled_package_count: usize,
    pub wads_built: Vec<String>,
    pub wads_reused: Vec<String>,
    pub conflict_count: usize,
    pub linked_bin_offender_count: usize,
    pub mod_reports: Vec<ltk_overlay::ModWadReport>,
    pub build_time_ms: u64,
}

#[derive(Debug, Serialize)]
struct OverlayProgressEvent {
    stage: &'static str,
    current_file: Option<String>,
    current: u32,
    total: u32,
}

pub fn build_overlay<W>(
    params: BuildOverlayParams,
    request_id: &str,
    emitter: &Emitter<W>,
) -> Result<BuildOverlayResult, EngineError>
where
    W: Write + Send + 'static,
{
    ensure_enabled_package_count(params.enabled_packages.len())?;
    let game_dir = canonical_existing_dir(&params.game_dir, "game_dir")?;
    let data_final = game_dir.join("DATA").join("FINAL");
    if !data_final.is_dir() {
        return Err(EngineError::invalid_path(format!(
            "game_dir must contain DATA/FINAL: {}",
            game_dir.display()
        )));
    }

    let game_dir_utf8 = utf8_path(game_dir.clone(), "game_dir")?;
    let enabled_package_count = params.enabled_packages.len();
    let enabled_mods = open_enabled_mods(params.enabled_packages)?;
    let (overlay_dir, state_dir) =
        prepare_output_dirs(&game_dir, &params.overlay_dir, &params.state_dir)?;

    let overlay_dir = utf8_path(overlay_dir, "overlay_dir")?;
    let state_dir = utf8_path(state_dir, "state_dir")?;

    let event_emitter = emitter.clone();
    let event_request_id = request_id.to_owned();
    let mut builder =
        OverlayBuilder::new(game_dir_utf8, overlay_dir, state_dir).with_progress(move |progress| {
            // A closed consumer is detected when the final response is written. The
            // upstream callback cannot return an I/O error, so progress is best-effort.
            let _ = event_emitter.event(
                &event_request_id,
                "overlay.progress",
                &stable_progress(progress),
            );
        });
    builder.set_enabled_mods(enabled_mods);

    let result = builder.build().map_err(|error| {
        EngineError::new(
            "overlay_build_failed",
            format!("LTK overlay build failed: {error}"),
        )
    })?;
    let mod_reports = builder.take_mod_wad_reports();
    let linked_bin_offender_count = builder.take_linked_bin_offenders().len();

    Ok(BuildOverlayResult {
        overlay_root: conventional_path(result.overlay_root.as_std_path()),
        enabled_package_count,
        wads_built: result
            .wads_built
            .into_iter()
            .map(|path| conventional_path(path.as_std_path()))
            .collect(),
        wads_reused: result
            .wads_reused
            .into_iter()
            .map(|path| conventional_path(path.as_std_path()))
            .collect(),
        conflict_count: result.conflicts.len(),
        linked_bin_offender_count,
        mod_reports,
        build_time_ms: u64::try_from(result.build_time.as_millis()).unwrap_or(u64::MAX),
    })
}

fn stable_progress(progress: OverlayProgress) -> OverlayProgressEvent {
    let stage = match progress.stage {
        OverlayStage::Indexing => "indexing",
        OverlayStage::CollectingOverrides => "collecting_overrides",
        OverlayStage::PatchingWad => "patching_wad",
        OverlayStage::ApplyingStringOverrides => "applying_string_overrides",
        OverlayStage::Complete => "complete",
    };
    OverlayProgressEvent {
        stage,
        current_file: progress.current_file,
        current: progress.current,
        total: progress.total,
    }
}

fn open_enabled_mods(raw_paths: Vec<String>) -> Result<Vec<EnabledMod>, EngineError> {
    ensure_enabled_package_count(raw_paths.len())?;
    let mut seen = HashSet::new();
    let mut enabled = Vec::with_capacity(raw_paths.len());
    for (index, raw_path) in raw_paths.into_iter().enumerate() {
        if raw_path.trim().is_empty() {
            return Err(EngineError::invalid_params(format!(
                "enabled_packages[{index}] must not be empty"
            )));
        }
        let path = PathBuf::from(&raw_path).canonicalize().map_err(|error| {
            EngineError::invalid_path(format!(
                "cannot resolve enabled_packages[{index}] '{raw_path}': {error}"
            ))
        })?;
        if !path.is_file() {
            return Err(EngineError::invalid_path(format!(
                "enabled_packages[{index}] is not a file: {}",
                path.display()
            )));
        }
        if !seen.insert(path.clone()) {
            return Err(EngineError::invalid_params(format!(
                "duplicate package path at enabled_packages[{index}]: {}",
                path.display()
            )));
        }

        let format = package_format(&path)?;
        let file = File::open(&path).map_err(|error| {
            EngineError::invalid_path(format!("cannot open package '{}': {error}", path.display()))
        })?;
        let file_size = file.metadata().map_err(|error| {
            EngineError::invalid_path(format!(
                "cannot read package metadata '{}': {error}",
                path.display()
            ))
        })?;
        ensure_package_file_size(&path, file_size.len())?;
        let utf8_archive = utf8_path(path.clone(), "enabled package")?;
        let content: Box<dyn ModContentProvider> = match format {
            PackageFormat::Fantome => {
                let archive = ZipArchive::new(file).map_err(|error| {
                    EngineError::new(
                        "package_open_failed",
                        format!("failed to open fantome '{}': {error}", path.display()),
                    )
                })?;
                ensure_fantome_entry_count(&path, archive.len())?;
                Box::new(
                    FantomeContent::new(archive.into_inner())
                        .map_err(|error| {
                            EngineError::new(
                                "package_open_failed",
                                format!("failed to open fantome '{}': {error}", path.display()),
                            )
                        })?
                        .with_archive_path(utf8_archive),
                )
            }
            PackageFormat::Modpkg => {
                let package = Modpkg::mount_from_reader(file).map_err(|error| {
                    EngineError::new(
                        "package_open_failed",
                        format!("failed to mount modpkg '{}': {error}", path.display()),
                    )
                })?;
                Box::new(ModpkgContent::new(package).with_archive_path(utf8_archive))
            }
        };
        enabled.push(EnabledMod {
            // Order is intentionally part of the stable state identifier: item 0
            // has the highest priority in ltk_overlay.
            id: format!("{index}:{}", conventional_path(&path)),
            content,
            enabled_layers: None,
        });
    }
    Ok(enabled)
}

fn ensure_enabled_package_count(count: usize) -> Result<(), EngineError> {
    if count > MAX_ENABLED_PACKAGES {
        return Err(EngineError::invalid_params(format!(
            "enabled_packages may contain at most {MAX_ENABLED_PACKAGES} paths"
        )));
    }
    Ok(())
}

fn canonical_existing_dir(raw: &str, field: &str) -> Result<PathBuf, EngineError> {
    if raw.trim().is_empty() {
        return Err(EngineError::invalid_params(format!(
            "{field} must not be empty"
        )));
    }
    let path = PathBuf::from(raw);
    let canonical = path.canonicalize().map_err(|error| {
        EngineError::invalid_path(format!(
            "cannot resolve {field} '{}': {error}",
            path.display()
        ))
    })?;
    if !canonical.is_dir() {
        return Err(EngineError::invalid_path(format!(
            "{field} is not a directory: {}",
            canonical.display()
        )));
    }
    Ok(canonical)
}

fn resolve_output_candidate(raw: &str, field: &str) -> Result<PathBuf, EngineError> {
    if raw.trim().is_empty() {
        return Err(EngineError::invalid_params(format!(
            "{field} must not be empty"
        )));
    }
    let raw_path = PathBuf::from(raw);
    if raw_path.exists() {
        return canonical_existing_dir(raw_path.to_string_lossy().as_ref(), field);
    }

    let absolute = if raw_path.is_absolute() {
        raw_path
    } else {
        std::env::current_dir()
            .map_err(|error| {
                EngineError::invalid_path(format!(
                    "cannot resolve current directory for {field}: {error}"
                ))
            })?
            .join(raw_path)
    };
    let leaf = absolute
        .file_name()
        .ok_or_else(|| EngineError::invalid_path(format!("{field} must name a child directory")))?;
    let parent = absolute.parent().ok_or_else(|| {
        EngineError::invalid_path(format!("{field} must have an existing parent directory"))
    })?;
    let canonical_parent = parent.canonicalize().map_err(|error| {
        EngineError::invalid_path(format!(
            "cannot resolve existing parent of {field} '{}': {error}",
            parent.display()
        ))
    })?;
    if !canonical_parent.is_dir() {
        return Err(EngineError::invalid_path(format!(
            "parent of {field} is not a directory: {}",
            canonical_parent.display()
        )));
    }
    Ok(canonical_parent.join(leaf))
}

fn prepare_output_dirs(
    game_dir: &Path,
    overlay_raw: &str,
    state_raw: &str,
) -> Result<(PathBuf, PathBuf), EngineError> {
    // Resolve and validate both destinations before creating either one. This
    // keeps rejected requests side-effect free, including paths inside Game/.
    let overlay_candidate = resolve_output_candidate(overlay_raw, "overlay_dir")?;
    let state_candidate = resolve_output_candidate(state_raw, "state_dir")?;
    validate_output_boundaries(game_dir, &overlay_candidate, &state_candidate)?;
    // The upstream crates require UTF-8 paths. Reject before creating leaves.
    let _ = utf8_path(overlay_candidate.clone(), "overlay_dir")?;
    let _ = utf8_path(state_candidate.clone(), "state_dir")?;

    let overlay_created = create_leaf_dir(&overlay_candidate, "overlay_dir")?;
    let state_created = match create_leaf_dir(&state_candidate, "state_dir") {
        Ok(created) => created,
        Err(error) => {
            rollback_created_dirs(&[(overlay_created, &overlay_candidate)]);
            return Err(error);
        }
    };

    let prepared = (|| {
        let overlay_dir =
            canonical_existing_dir(overlay_candidate.to_string_lossy().as_ref(), "overlay_dir")?;
        let state_dir =
            canonical_existing_dir(state_candidate.to_string_lossy().as_ref(), "state_dir")?;
        // Recheck canonical destinations after creation to guard against aliases.
        validate_output_boundaries(game_dir, &overlay_dir, &state_dir)?;
        Ok((overlay_dir, state_dir))
    })();
    if prepared.is_err() {
        // Remove only leaf directories this request created, in reverse order,
        // and only if they are still empty. Pre-existing paths are untouched.
        rollback_created_dirs(&[
            (state_created, &state_candidate),
            (overlay_created, &overlay_candidate),
        ]);
    }
    prepared
}

fn create_leaf_dir(path: &Path, field: &str) -> Result<bool, EngineError> {
    match std::fs::create_dir(path) {
        Ok(()) => Ok(true),
        Err(error) if error.kind() == std::io::ErrorKind::AlreadyExists && path.is_dir() => {
            Ok(false)
        }
        Err(error) => Err(EngineError::invalid_path(format!(
            "cannot create {field} '{}': {error}",
            path.display()
        ))),
    }
}

fn rollback_created_dirs(created: &[(bool, &Path)]) {
    for (was_created, path) in created {
        if *was_created {
            // remove_dir is intentionally non-recursive: if another actor placed
            // content here, preserving it is safer than completing the rollback.
            let _ = std::fs::remove_dir(path);
        }
    }
}

fn validate_output_boundaries(
    game_dir: &Path,
    overlay_dir: &Path,
    state_dir: &Path,
) -> Result<(), EngineError> {
    if overlay_dir.starts_with(state_dir) || state_dir.starts_with(overlay_dir) {
        return Err(EngineError::invalid_path(
            "overlay_dir and state_dir must not overlap",
        ));
    }
    for (field, output) in [("overlay_dir", overlay_dir), ("state_dir", state_dir)] {
        if output.starts_with(game_dir) || game_dir.starts_with(output) {
            return Err(EngineError::invalid_path(format!(
                "{field} must not overlap game_dir"
            )));
        }
    }
    Ok(())
}

fn utf8_path(path: PathBuf, field: &str) -> Result<Utf8PathBuf, EngineError> {
    Utf8PathBuf::from_path_buf(path).map_err(|path| {
        EngineError::invalid_path(format!(
            "{field} must be valid UTF-8 for the LTK engine: {}",
            path.display()
        ))
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn rejected_game_overlap_creates_nothing() {
        let root = tempfile::tempdir().unwrap();
        let game = root.path().join("game");
        std::fs::create_dir(&game).unwrap();
        let game = game.canonicalize().unwrap();
        let unsafe_overlay = game.join("overlay");
        let safe_state = root.path().join("state");

        assert!(
            prepare_output_dirs(
                &game,
                unsafe_overlay.to_str().unwrap(),
                safe_state.to_str().unwrap(),
            )
            .is_err()
        );
        assert!(!unsafe_overlay.exists());
        assert!(!safe_state.exists());
    }

    #[test]
    fn rejected_output_overlap_creates_nothing() {
        let root = tempfile::tempdir().unwrap();
        let game = root.path().join("game");
        let profile = root.path().join("profile");
        std::fs::create_dir(&game).unwrap();
        std::fs::create_dir(&profile).unwrap();
        let game = game.canonicalize().unwrap();
        let profile = profile.canonicalize().unwrap();
        let nested_overlay = profile.join("overlay");

        assert!(
            prepare_output_dirs(
                &game,
                nested_overlay.to_str().unwrap(),
                profile.to_str().unwrap(),
            )
            .is_err()
        );
        assert!(!nested_overlay.exists());
    }

    #[test]
    fn second_creation_failure_rolls_back_only_new_first_leaf() {
        let root = tempfile::tempdir().unwrap();
        let game = root.path().join("game");
        std::fs::create_dir(&game).unwrap();
        let game = game.canonicalize().unwrap();
        let overlay = root.path().join("new-overlay");
        let invalid_state = root.path().join("x".repeat(300));

        assert!(
            prepare_output_dirs(
                &game,
                overlay.to_str().unwrap(),
                invalid_state.to_str().unwrap(),
            )
            .is_err()
        );
        assert!(!overlay.exists());
        assert!(!invalid_state.exists());
    }

    #[test]
    fn second_creation_failure_preserves_preexisting_first_leaf() {
        let root = tempfile::tempdir().unwrap();
        let game = root.path().join("game");
        let overlay = root.path().join("existing-overlay");
        std::fs::create_dir(&game).unwrap();
        std::fs::create_dir(&overlay).unwrap();
        let game = game.canonicalize().unwrap();
        let invalid_state = root.path().join("x".repeat(300));

        assert!(
            prepare_output_dirs(
                &game,
                overlay.to_str().unwrap(),
                invalid_state.to_str().unwrap(),
            )
            .is_err()
        );
        assert!(overlay.is_dir());
    }

    #[test]
    fn stable_progress_uses_protocol_owned_names() {
        let value = stable_progress(OverlayProgress {
            stage: OverlayStage::CollectingOverrides,
            current_file: None,
            current: 0,
            total: 0,
        });
        assert_eq!(value.stage, "collecting_overrides");
    }

    #[test]
    fn enabled_package_count_is_bounded_before_opening_paths() {
        assert!(ensure_enabled_package_count(MAX_ENABLED_PACKAGES).is_ok());
        let error = ensure_enabled_package_count(MAX_ENABLED_PACKAGES + 1).unwrap_err();
        assert_eq!(error.code(), "invalid_params");

        let paths = vec!["does-not-exist.modpkg".to_owned(); MAX_ENABLED_PACKAGES + 1];
        let error = match open_enabled_mods(paths) {
            Ok(_) => panic!("oversized enabled package list was accepted"),
            Err(error) => error,
        };
        assert!(error.message().contains("at most"));
    }
}
