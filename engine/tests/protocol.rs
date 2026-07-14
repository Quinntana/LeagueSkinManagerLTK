// SPDX-License-Identifier: MIT OR Apache-2.0

use ltk_engine::{Emitter, serve};
use ltk_modpkg::builder::{ModpkgBuilder, ModpkgChunkBuilder, ModpkgLayerBuilder};
use ltk_modpkg::{ModpkgAuthor, ModpkgMetadata};
use serde_json::{Value, json};
use std::io::{Cursor, Write};
use std::sync::{Arc, Mutex};
use zip::write::SimpleFileOptions;

#[derive(Clone, Default)]
struct SharedBuffer(Arc<Mutex<Vec<u8>>>);

impl Write for SharedBuffer {
    fn write(&mut self, bytes: &[u8]) -> std::io::Result<usize> {
        self.0.lock().unwrap().extend_from_slice(bytes);
        Ok(bytes.len())
    }

    fn flush(&mut self) -> std::io::Result<()> {
        Ok(())
    }
}

impl SharedBuffer {
    fn frames(&self) -> Vec<Value> {
        let bytes = self.0.lock().unwrap().clone();
        String::from_utf8(bytes)
            .unwrap()
            .lines()
            .map(|line| serde_json::from_str(line).unwrap())
            .collect()
    }
}

fn run_requests(requests: &[Value]) -> Vec<Value> {
    let mut input = Vec::new();
    for request in requests {
        serde_json::to_writer(&mut input, request).unwrap();
        input.push(b'\n');
    }
    let output = SharedBuffer::default();
    serve(Cursor::new(input), Emitter::new(output.clone())).unwrap();
    output.frames()
}

#[test]
fn hello_uses_versioned_correlated_response_contract() {
    let frames = run_requests(&[json!({
        "protocol": 1,
        "id": "hello-1",
        "method": "engine.hello",
        "params": {}
    })]);
    assert_eq!(frames.len(), 1);
    assert_eq!(frames[0]["protocol"], 1);
    assert_eq!(frames[0]["id"], "hello-1");
    assert_eq!(frames[0]["type"], "response");
    assert_eq!(frames[0]["ok"], true);
    assert_eq!(
        frames[0]["result"]["crate_versions"]["ltk_overlay"],
        "0.5.2"
    );
    assert_eq!(frames[0]["result"]["provider"]["binaries_bundled"], false);
}

#[test]
fn malformed_request_does_not_end_the_stream() {
    let output = SharedBuffer::default();
    let input = br#"not json
{"protocol":1,"id":"next","method":"engine.hello","params":{}}
"#;
    serve(Cursor::new(input), Emitter::new(output.clone())).unwrap();
    let frames = output.frames();
    assert_eq!(frames.len(), 2);
    assert_eq!(frames[0]["ok"], false);
    assert_eq!(frames[0]["error"]["code"], "invalid_request");
    assert_eq!(frames[1]["id"], "next");
    assert_eq!(frames[1]["ok"], true);
}

#[test]
fn protocol_and_params_are_strict() {
    let frames = run_requests(&[
        json!({"protocol": 2, "id": "version", "method": "engine.hello", "params": {}}),
        json!({"protocol": 1, "id": "method", "method": "missing", "params": {}}),
        json!({"protocol": 1, "id": "flags", "method": "provider.smoke", "params": {
            "installation_dir": "x", "overlay_prefix": "y", "flags": 4
        }}),
    ]);
    assert_eq!(frames[0]["error"]["code"], "unsupported_protocol");
    assert_eq!(frames[1]["error"]["code"], "method_not_found");
    assert_eq!(frames[2]["error"]["code"], "invalid_params");
}

#[test]
fn inspects_synthetic_fantome_metadata() {
    let directory = tempfile::tempdir().unwrap();
    let path = directory.path().join("synthetic.fantome");
    let file = std::fs::File::create(&path).unwrap();
    let mut archive = zip::ZipWriter::new(file);
    let options = SimpleFileOptions::default().compression_method(zip::CompressionMethod::Stored);
    archive.start_file("META/info.json", options).unwrap();
    archive
        .write_all(
            br#"{
                "Name":"Synthetic Fantome",
                "Author":"Fixture Author",
                "Version":"1.2.3",
                "Description":"Protocol fixture",
                "Tags":["sfx"],
                "Champions":["Ahri"],
                "Maps":[],
                "Layers":{}
            }"#,
        )
        .unwrap();
    archive
        .start_file(
            "WAD/Ahri.wad.client/assets/characters/ahri/test.bin",
            options,
        )
        .unwrap();
    archive.write_all(b"fixture").unwrap();
    archive.finish().unwrap();

    let frames = run_requests(&[json!({
        "protocol": 1,
        "id": "inspect-fantome",
        "method": "package.inspect",
        "params": {"path": path}
    })]);
    let result = &frames[0]["result"];
    assert_eq!(frames[0]["ok"], true);
    assert_eq!(result["format"], "fantome");
    assert_eq!(result["display_name"], "Synthetic Fantome");
    assert_eq!(result["authors"][0]["name"], "Fixture Author");
    assert_eq!(result["champions"][0], "Ahri");
    assert_eq!(result["tags"][0], "sfx");
    assert!(!result["path"].as_str().unwrap().starts_with(r"\\?\"));
}

#[test]
fn inspects_synthetic_modpkg_with_official_builder() {
    let directory = tempfile::tempdir().unwrap();
    let path = directory.path().join("synthetic.modpkg");
    let metadata = ModpkgMetadata {
        name: "synthetic-modpkg".to_owned(),
        display_name: "Synthetic Modpkg".to_owned(),
        version: "2.3.4".parse().unwrap(),
        authors: vec![ModpkgAuthor::new("Fixture Author".to_owned(), None)],
        tags: vec!["ui".to_owned()],
        champions: vec!["Lux".to_owned()],
        ..ModpkgMetadata::default()
    };

    let builder = ModpkgBuilder::default()
        .with_metadata(metadata)
        .unwrap()
        .with_layer(ModpkgLayerBuilder::base())
        .with_chunk(
            ModpkgChunkBuilder::new()
                .with_path("assets/characters/lux/test.bin")
                .unwrap()
                .with_layer("base")
                .with_wad("Lux.wad.client"),
        );
    let mut file = std::fs::File::create(&path).unwrap();
    builder
        .build_to_writer(&mut file, |_chunk, writer| {
            writer.write_all(b"fixture payload")?;
            Ok(())
        })
        .unwrap();

    let frames = run_requests(&[json!({
        "protocol": 1,
        "id": "inspect-modpkg",
        "method": "package.inspect",
        "params": {"path": path}
    })]);
    let result = &frames[0]["result"];
    assert_eq!(frames[0]["ok"], true);
    assert_eq!(result["format"], "modpkg");
    assert_eq!(result["display_name"], "Synthetic Modpkg");
    assert_eq!(result["version"], "2.3.4");
    assert_eq!(result["file_count"], 1);
    assert_eq!(result["wads"][0], "lux.wad.client");
    assert!(!result["path"].as_str().unwrap().starts_with(r"\\?\"));
}

#[test]
fn builds_and_cleans_empty_overlay_fixture_without_injection() {
    let directory = tempfile::tempdir().unwrap();
    let game = directory.path().join("game");
    let overlay = directory.path().join("overlay");
    let state = directory.path().join("state");
    std::fs::create_dir_all(game.join("DATA").join("FINAL")).unwrap();

    let frames = run_requests(&[json!({
        "protocol": 1,
        "id": "overlay-empty",
        "method": "overlay.build",
        "params": {
            "game_dir": game,
            "overlay_dir": overlay,
            "state_dir": state,
            "enabled_packages": []
        }
    })]);
    assert!(
        frames.len() >= 2,
        "expected progress events plus a response"
    );
    assert!(
        frames[..frames.len() - 1]
            .iter()
            .all(|frame| frame["type"] == "event" && frame["id"] == "overlay-empty")
    );
    let response = frames.last().unwrap();
    assert_eq!(response["ok"], true);
    assert_eq!(response["result"]["enabled_package_count"], 0);
    assert_eq!(response["result"]["wads_built"], json!([]));
    assert!(
        !response["result"]["overlay_root"]
            .as_str()
            .unwrap()
            .starts_with(r"\\?\")
    );
}
