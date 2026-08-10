import { invoke, isTauri } from "@tauri-apps/api/core";
import { listen, type UnlistenFn } from "@tauri-apps/api/event";

/**
 * Rust側 `SetupState`（src-tauri/src/main.rs）とフィールド名・値を完全に一致させること。
 * `.venv` が無い初回起動時、デスクトップシェルが `npm run setup:raw` 相当の
 * ブートストラップ（venv作成 → pip install）を自前で実行する際の進捗を表す。
 */
export type SetupStage = "ready" | "creating-venv" | "installing-deps" | "succeeded" | "failed";

export type SetupState = {
  stage: SetupStage;
  message: string;
  log: string[];
};

const SETUP_EVENT = "raw-setup";

export { isTauri };

/** `invoke("setup_status")` — 現在のブートストラップ状態を1回だけ取得する（pull）。 */
export async function getSetupStatus(): Promise<SetupState> {
  return await invoke<SetupState>("setup_status");
}

/**
 * `"raw-setup"` イベントを購読する（push）。Rust側は状態が変わるたびにこのイベントを
 * 発火するため、マウント済みの画面はこちらで即座に更新を受け取れる。
 * 返り値の解除関数は呼び出し側で unmount 時に必ず呼ぶこと。
 */
export async function onSetupUpdate(handler: (state: SetupState) => void): Promise<UnlistenFn> {
  return await listen<SetupState>(SETUP_EVENT, (event) => handler(event.payload));
}
