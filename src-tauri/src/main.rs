// Prevents additional console window on Windows in release, DO NOT REMOVE!!
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

//! GeoFrame RAW Assist desktop shell.

mod adjustments;
mod analysis;
mod bracket;
mod candidates;
mod coreimage;
mod exif_inherit;
mod export;
mod photos_import;
mod presets;
mod source_folder;

fn main() {
    tauri::Builder::default()
        .manage(analysis::AnalysisState::default())
        // 元 JPG の配信（Issue #110）: geoframe://localhost/source-jpg/DSC12345.JPG
        .register_asynchronous_uri_scheme_protocol("geoframe", analysis::source_jpg_protocol)
        .invoke_handler(tauri::generate_handler![
            // ブラケット合成（Issue #84）。
            bracket::merge_bracket,
            // Core Image プレビュー・原寸現像（Issue #108）。
            coreimage::develop_full_coreimage,
            coreimage::develop_preview_coreimage,
            // エンジン情報（Issue #109）。
            coreimage::engine_info,
            presets::get_presets,
            candidates::command::generate_development_candidates,
            analysis::get_analysis,
            analysis::start_analysis,
            source_folder::get_source_folder,
            source_folder::set_source_folder,
            export::export_selection,
            export::reveal_export,
            photos_import::import_to_photos
        ])
        .plugin(tauri_plugin_dialog::init())
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}
