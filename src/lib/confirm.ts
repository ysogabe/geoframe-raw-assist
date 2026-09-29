import { confirm } from "@tauri-apps/plugin-dialog";
import { isTauri } from "./setup";

/**
 * 手動調整を捨てる操作の確認ダイアログ。OK なら true。
 *
 * Tauri の WKWebView（macOS .app）では window.confirm がダイアログを出さずに
 * 即座に返ってしまうため、デスクトップでは tauri-plugin-dialog のネイティブ
 * ダイアログを使う（capabilities で dialog:allow-confirm を許可済み）。
 * ブラウザモード（vite dev）では従来どおり window.confirm。
 *
 * 例外は呼び出し側へそのまま投げる（確認できなかった＝移動しない、の判断は呼び出し側）。
 */
export async function confirmDiscard(message: string): Promise<boolean> {
  if (isTauri()) {
    return await confirm(message, {
      title: "調整値の破棄",
      kind: "warning",
      okLabel: "移動する",
      cancelLabel: "キャンセル",
    });
  }
  return window.confirm(message);
}
