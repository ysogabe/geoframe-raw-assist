import { confirm } from "@tauri-apps/plugin-dialog";

/**
 * 手動調整を捨てる操作の確認ダイアログ。OK なら true。
 *
 * Tauri の WKWebView（macOS .app）では window.confirm がダイアログを出さずに
 * 即座に返ってしまうため、tauri-plugin-dialog のネイティブダイアログを使う
 * （capabilities で dialog:allow-confirm を許可済み）。
 *
 * 例外は呼び出し側へそのまま投げる（確認できなかった＝移動しない、の判断は呼び出し側）。
 */
export async function confirmDiscard(message: string): Promise<boolean> {
  return await confirm(message, {
    title: "調整値の破棄",
    kind: "warning",
    okLabel: "移動する",
    cancelLabel: "キャンセル",
  });
}
