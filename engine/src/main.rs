// SPDX-License-Identifier: MIT OR Apache-2.0

use ltk_engine::{Emitter, serve};
use std::io;

fn main() {
    let stdin = io::stdin();
    let emitter = Emitter::new(io::stdout());
    if let Err(error) = serve(stdin.lock(), emitter) {
        // stdout is reserved exclusively for protocol frames.
        eprintln!("ltk-engine protocol stopped: {error}");
        std::process::exit(1);
    }
}
