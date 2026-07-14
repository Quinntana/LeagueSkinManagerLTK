// SPDX-License-Identifier: MIT OR Apache-2.0

use crate::error::EngineError;
use crate::path_output::conventional_path;
use ltk_fantome::FantomeInfo;
use ltk_modpkg::{Modpkg, ModpkgLicense};
use serde::{Deserialize, Serialize};
use std::collections::{BTreeMap, BTreeSet};
use std::fs::File;
use std::io::Read;
use std::path::{Path, PathBuf};
use zip::ZipArchive;

const MAX_FANTOME_METADATA_BYTES: u64 = 1024 * 1024;
pub(crate) const MAX_PACKAGE_FILE_BYTES: u64 = 2 * 1024 * 1024 * 1024;
pub(crate) const MAX_FANTOME_ENTRIES: usize = 100_000;

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct InspectPackageParams {
    pub path: String,
}

#[derive(Debug, Clone, Copy, Serialize, PartialEq, Eq)]
#[serde(rename_all = "lowercase")]
pub enum PackageFormat {
    Modpkg,
    Fantome,
}

#[derive(Debug, Serialize, PartialEq, Eq)]
pub struct PackageAuthor {
    pub name: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub role: Option<String>,
}

#[derive(Debug, Serialize, PartialEq, Eq)]
pub struct PackageLayer {
    pub name: String,
    pub priority: i32,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub description: Option<String>,
    pub file_count: u64,
}

#[derive(Debug, Serialize)]
pub struct PackageInspection {
    pub format: PackageFormat,
    pub path: String,
    pub file_size: u64,
    pub name: String,
    pub display_name: String,
    pub version: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub description: Option<String>,
    pub authors: Vec<PackageAuthor>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub license: Option<String>,
    pub tags: Vec<String>,
    pub champions: Vec<String>,
    pub maps: Vec<String>,
    pub layers: Vec<PackageLayer>,
    pub wads: Vec<String>,
    pub file_count: u64,
    pub total_uncompressed_size: u64,
}

pub fn inspect_package(path: &str) -> Result<PackageInspection, EngineError> {
    let path = canonical_package_path(path)?;
    let format = package_format(&path)?;
    match format {
        PackageFormat::Modpkg => inspect_modpkg(&path),
        PackageFormat::Fantome => inspect_fantome(&path),
    }
}

fn canonical_package_path(raw: &str) -> Result<PathBuf, EngineError> {
    if raw.trim().is_empty() {
        return Err(EngineError::invalid_params(
            "package path must not be empty",
        ));
    }
    let path = PathBuf::from(raw);
    let canonical = path.canonicalize().map_err(|error| {
        EngineError::invalid_path(format!(
            "cannot resolve package '{}': {error}",
            path.display()
        ))
    })?;
    let metadata = canonical.metadata().map_err(|error| {
        EngineError::invalid_path(format!(
            "cannot read package metadata '{}': {error}",
            canonical.display()
        ))
    })?;
    if !metadata.is_file() {
        return Err(EngineError::invalid_path(format!(
            "package is not a file: {}",
            canonical.display()
        )));
    }
    ensure_package_file_size(&canonical, metadata.len())?;
    Ok(canonical)
}

pub(crate) fn ensure_package_file_size(path: &Path, size: u64) -> Result<(), EngineError> {
    if size > MAX_PACKAGE_FILE_BYTES {
        return Err(EngineError::new(
            "package_too_large",
            format!(
                "package '{}' is {size} bytes; maximum supported size is {MAX_PACKAGE_FILE_BYTES} bytes",
                path.display()
            ),
        ));
    }
    Ok(())
}

pub(crate) fn ensure_fantome_entry_count(
    path: &Path,
    entry_count: usize,
) -> Result<(), EngineError> {
    if entry_count > MAX_FANTOME_ENTRIES {
        return Err(EngineError::new(
            "package_too_many_entries",
            format!(
                "fantome '{}' contains {entry_count} entries; maximum supported count is {MAX_FANTOME_ENTRIES}",
                path.display()
            ),
        ));
    }
    Ok(())
}

pub(crate) fn package_format(path: &Path) -> Result<PackageFormat, EngineError> {
    let extension = path
        .extension()
        .and_then(|value| value.to_str())
        .map(str::to_ascii_lowercase);
    match extension.as_deref() {
        Some("modpkg") => Ok(PackageFormat::Modpkg),
        Some("fantome") => Ok(PackageFormat::Fantome),
        _ => Err(EngineError::new(
            "unsupported_package_format",
            format!(
                "unsupported package '{}'; expected .modpkg or .fantome",
                path.display()
            ),
        )),
    }
}

fn inspect_modpkg(path: &Path) -> Result<PackageInspection, EngineError> {
    let file = File::open(path).map_err(|error| package_read_error(path, &error))?;
    let file_size = file
        .metadata()
        .map_err(|error| package_read_error(path, &error))?
        .len();
    ensure_package_file_size(path, file_size)?;
    let mut package = Modpkg::mount_from_reader(file).map_err(|error| {
        EngineError::new(
            "package_inspection_failed",
            format!("failed to mount modpkg '{}': {error}", path.display()),
        )
    })?;
    let metadata = package.load_metadata().map_err(|error| {
        EngineError::new(
            "package_inspection_failed",
            format!(
                "failed to read modpkg metadata '{}': {error}",
                path.display()
            ),
        )
    })?;

    let mut file_count = 0_u64;
    let mut total_uncompressed_size = 0_u64;
    let mut layer_counts: BTreeMap<String, u64> = BTreeMap::new();
    for ((path_hash, layer_hash), chunk) in &package.chunks {
        let chunk_path = package
            .chunk_paths
            .get(path_hash)
            .map_or("", String::as_str);
        if chunk_path.to_ascii_lowercase().starts_with("_meta_/") {
            continue;
        }
        file_count = file_count.saturating_add(1);
        total_uncompressed_size = total_uncompressed_size.saturating_add(chunk.uncompressed_size);
        if let Some(layer) = package.layers.get(layer_hash) {
            *layer_counts.entry(layer.name.clone()).or_default() += 1;
        }
    }

    let mut layers: Vec<PackageLayer> = package
        .layers
        .values()
        .map(|layer| {
            let layer_metadata = metadata.layers.iter().find(|item| item.name == layer.name);
            PackageLayer {
                name: layer.name.clone(),
                priority: layer.priority,
                description: layer_metadata.and_then(|item| item.description.clone()),
                file_count: layer_counts.get(&layer.name).copied().unwrap_or_default(),
            }
        })
        .collect();
    layers.sort_by(|left, right| {
        left.priority
            .cmp(&right.priority)
            .then_with(|| left.name.cmp(&right.name))
    });

    let mut wads: Vec<String> = package.wads.values().cloned().collect();
    wads.sort_by_key(|value| value.to_ascii_lowercase());
    wads.dedup_by(|left, right| left.eq_ignore_ascii_case(right));

    Ok(PackageInspection {
        format: PackageFormat::Modpkg,
        path: conventional_path(path),
        file_size,
        name: metadata.name,
        display_name: metadata.display_name,
        version: metadata.version.to_string(),
        description: metadata
            .description
            .filter(|value| !value.trim().is_empty()),
        authors: metadata
            .authors
            .into_iter()
            .map(|author| PackageAuthor {
                name: author.name,
                role: author.role,
            })
            .collect(),
        license: modpkg_license(&metadata.license),
        tags: metadata.tags,
        champions: metadata.champions,
        maps: metadata.maps,
        layers,
        wads,
        file_count,
        total_uncompressed_size,
    })
}

fn modpkg_license(license: &ModpkgLicense) -> Option<String> {
    match license {
        ModpkgLicense::None => None,
        ModpkgLicense::Spdx { spdx_id } => Some(spdx_id.clone()),
        ModpkgLicense::Custom { name, url } => Some(format!("{name} ({url})")),
    }
}

fn inspect_fantome(path: &Path) -> Result<PackageInspection, EngineError> {
    let file = File::open(path).map_err(|error| package_read_error(path, &error))?;
    let file_size = file
        .metadata()
        .map_err(|error| package_read_error(path, &error))?
        .len();
    ensure_package_file_size(path, file_size)?;
    let mut archive = ZipArchive::new(file).map_err(|error| {
        EngineError::new(
            "package_inspection_failed",
            format!("failed to open fantome '{}': {error}", path.display()),
        )
    })?;
    ensure_fantome_entry_count(path, archive.len())?;

    let mut metadata_bytes: Option<Vec<u8>> = None;
    let mut file_count = 0_u64;
    let mut total_uncompressed_size = 0_u64;
    let mut wads = BTreeSet::new();

    for index in 0..archive.len() {
        let mut entry = archive.by_index(index).map_err(|error| {
            EngineError::new(
                "package_inspection_failed",
                format!(
                    "failed to read fantome entry #{index} in '{}': {error}",
                    path.display()
                ),
            )
        })?;
        let name = entry.name().to_owned();
        if entry.is_dir() {
            continue;
        }

        let is_metadata = name
            .get(..5)
            .is_some_and(|prefix| prefix.eq_ignore_ascii_case("META/"));
        if !is_metadata {
            file_count = file_count.saturating_add(1);
            total_uncompressed_size = total_uncompressed_size.saturating_add(entry.size());
        }
        if let Some(wad) = fantome_wad_name(&name) {
            wads.insert(wad.to_ascii_lowercase());
        }

        if name.eq_ignore_ascii_case("META/info.json") {
            if entry.size() > MAX_FANTOME_METADATA_BYTES {
                return Err(EngineError::new(
                    "package_inspection_failed",
                    format!(
                        "fantome metadata exceeds {} bytes",
                        MAX_FANTOME_METADATA_BYTES
                    ),
                ));
            }
            let mut bytes = Vec::with_capacity(usize::try_from(entry.size()).unwrap_or(0));
            entry
                .by_ref()
                .take(MAX_FANTOME_METADATA_BYTES + 1)
                .read_to_end(&mut bytes)
                .map_err(|error| package_read_error(path, &error))?;
            metadata_bytes = Some(bytes);
        }
    }

    let bytes = metadata_bytes.ok_or_else(|| {
        EngineError::new(
            "package_inspection_failed",
            format!("fantome '{}' is missing META/info.json", path.display()),
        )
    })?;
    let content = std::str::from_utf8(&bytes).map_err(|error| {
        EngineError::new(
            "package_inspection_failed",
            format!("fantome metadata is not UTF-8: {error}"),
        )
    })?;
    let info: FantomeInfo = serde_json::from_str(content.trim_start_matches('\u{feff}').trim())
        .map_err(|error| {
            EngineError::new(
                "package_inspection_failed",
                format!("failed to parse fantome metadata: {error}"),
            )
        })?;

    let mut layers: Vec<PackageLayer> = info
        .layers
        .into_iter()
        .map(|(key, layer)| PackageLayer {
            name: if layer.name.is_empty() {
                key
            } else {
                layer.name
            },
            priority: layer.priority,
            description: None,
            file_count: 0,
        })
        .collect();
    if !layers.iter().any(|layer| layer.name == "base") {
        layers.push(PackageLayer {
            name: "base".to_owned(),
            priority: 0,
            description: None,
            file_count,
        });
    } else if let Some(base) = layers.iter_mut().find(|layer| layer.name == "base") {
        base.file_count = file_count;
    }
    layers.sort_by(|left, right| {
        left.priority
            .cmp(&right.priority)
            .then_with(|| left.name.cmp(&right.name))
    });

    Ok(PackageInspection {
        format: PackageFormat::Fantome,
        path: conventional_path(path),
        file_size,
        name: info.name.clone(),
        display_name: info.name,
        version: info.version,
        description: (!info.description.trim().is_empty()).then_some(info.description),
        authors: vec![PackageAuthor {
            name: info.author,
            role: None,
        }],
        license: None,
        tags: info.tags,
        champions: info.champions,
        maps: info.maps,
        layers,
        wads: wads.into_iter().collect(),
        file_count,
        total_uncompressed_size,
    })
}

fn fantome_wad_name(entry_name: &str) -> Option<&str> {
    let prefix = entry_name.get(..4)?;
    if !prefix.eq_ignore_ascii_case("WAD/") {
        return None;
    }
    let relative = entry_name.get(4..)?;
    let first = relative.split('/').next()?;
    let lower = first.to_ascii_lowercase();
    (lower.ends_with(".wad.client") || lower.ends_with(".wad") || lower.ends_with(".wad.mobile"))
        .then_some(first)
}

fn package_read_error(path: &Path, error: &std::io::Error) -> EngineError {
    EngineError::new(
        "package_inspection_failed",
        format!("failed to read package '{}': {error}", path.display()),
    )
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn package_size_limit_is_inclusive_and_bounded() {
        let path = Path::new("oversized.fantome");
        assert!(ensure_package_file_size(path, MAX_PACKAGE_FILE_BYTES).is_ok());
        let error = ensure_package_file_size(path, MAX_PACKAGE_FILE_BYTES + 1).unwrap_err();
        assert_eq!(error.code(), "package_too_large");
    }

    #[test]
    fn fantome_entry_limit_is_inclusive_and_bounded() {
        let path = Path::new("many-entries.fantome");
        assert!(ensure_fantome_entry_count(path, MAX_FANTOME_ENTRIES).is_ok());
        let error = ensure_fantome_entry_count(path, MAX_FANTOME_ENTRIES + 1).unwrap_err();
        assert_eq!(error.code(), "package_too_many_entries");
    }
}
