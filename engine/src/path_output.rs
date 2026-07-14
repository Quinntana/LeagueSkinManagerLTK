// SPDX-License-Identifier: MIT OR Apache-2.0

use std::path::Path;

/// Render a canonical path for external consumers without Windows' verbatim
/// prefix. Internal callers must continue using the original canonical path
/// for containment and identity checks.
pub(crate) fn conventional_path(path: &Path) -> String {
    conventional_path_text(path.to_string_lossy().as_ref())
}

fn conventional_path_text(value: &str) -> String {
    let Some(rest) = value.strip_prefix(r"\\?\") else {
        return value.to_owned();
    };

    if rest
        .get(..4)
        .is_some_and(|prefix| prefix.eq_ignore_ascii_case("UNC\\"))
    {
        let tail = &rest[4..];
        let mut components = tail.split(['\\', '/']);
        let server = components.next().unwrap_or_default();
        let share = components.next().unwrap_or_default();
        if !server.is_empty() && !share.is_empty() {
            return format!(r"\\{tail}");
        }
        return value.to_owned();
    }

    let bytes = rest.as_bytes();
    if bytes.len() >= 3
        && bytes[0].is_ascii_alphabetic()
        && bytes[1] == b':'
        && matches!(bytes[2], b'\\' | b'/')
    {
        return rest.to_owned();
    }

    // Namespaces such as GLOBALROOT and malformed UNC/drive paths cannot be
    // represented safely by merely dropping the prefix.
    value.to_owned()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn strips_verbatim_drive_prefix() {
        assert_eq!(
            conventional_path_text(r"\\?\C:\Games\League\Game"),
            r"C:\Games\League\Game"
        );
    }

    #[test]
    fn strips_valid_verbatim_unc_prefix() {
        assert_eq!(
            conventional_path_text(r"\\?\UNC\server\share\mods\skin.modpkg"),
            r"\\server\share\mods\skin.modpkg"
        );
        assert_eq!(
            conventional_path_text(r"\\?\unc\server\share"),
            r"\\server\share"
        );
    }

    #[test]
    fn preserves_paths_that_cannot_be_safely_simplified() {
        for value in [
            r"C:\Games\League",
            r"\\server\share\mods",
            r"\\?\GLOBALROOT\Device\HarddiskVolume1",
            r"\\?\UNC\server",
            r"\\?\C:relative",
        ] {
            assert_eq!(conventional_path_text(value), value);
        }
    }
}
