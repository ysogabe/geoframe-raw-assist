"""アプリデータディレクトリ解決と旧パス移行（`local_raw.paths`）の検証。

`paths` は他モジュールに依存しない葉ノードなので、テストも `config` / `drive_upload` の
ドメイン知識を持ち込まず、このモジュール単体の契約だけを見る。実ファイルは必ず tmpdir に
作り、`app_data_dir()` が指す**本物の**ユーザーディレクトリには一切書き込まない。
"""

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from local_raw import paths

#: root で走らせるとパーミッション検査が成立しない（root は 0o500 でも書けてしまう）。
RUNNING_AS_ROOT = hasattr(os, "geteuid") and os.geteuid() == 0


class AppDataDirTests(unittest.TestCase):
    """`sys.platform` と環境変数だけで決まる純粋な解決ロジック。

    `Path.home()` は実行環境で変わるので、期待値も `Path.home()` から組み立てて
    「どのマシンで走らせても同じ結論」になるようにする。
    """

    def test_macos_uses_application_support(self):
        with mock.patch.object(paths.sys, "platform", "darwin"):
            self.assertEqual(
                paths.app_data_dir(),
                Path.home() / "Library" / "Application Support" / paths.APP_IDENTIFIER,
            )

    def test_windows_uses_appdata_when_set(self):
        with (
            mock.patch.object(paths.sys, "platform", "win32"),
            mock.patch.dict(os.environ, {"APPDATA": r"C:\Users\test\AppData\Roaming"}, clear=False),
        ):
            self.assertEqual(
                paths.app_data_dir(),
                Path(r"C:\Users\test\AppData\Roaming") / paths.APP_IDENTIFIER,
            )

    def test_windows_falls_back_to_home_when_appdata_is_unset(self):
        with mock.patch.object(paths.sys, "platform", "win32"), mock.patch.dict(os.environ):
            # ambient な環境に APPDATA があってもなくても「未設定」を再現する。
            os.environ.pop("APPDATA", None)
            self.assertEqual(
                paths.app_data_dir(),
                Path.home() / "AppData" / "Roaming" / paths.APP_IDENTIFIER,
            )

    def test_linux_uses_xdg_data_home_when_set(self):
        with (
            mock.patch.object(paths.sys, "platform", "linux"),
            mock.patch.dict(os.environ, {"XDG_DATA_HOME": "/custom/share"}, clear=False),
        ):
            self.assertEqual(
                paths.app_data_dir(), Path("/custom/share") / paths.APP_IDENTIFIER
            )

    def test_linux_falls_back_to_local_share_when_xdg_is_unset(self):
        with mock.patch.object(paths.sys, "platform", "linux"), mock.patch.dict(os.environ):
            os.environ.pop("XDG_DATA_HOME", None)
            self.assertEqual(
                paths.app_data_dir(),
                Path.home() / ".local" / "share" / paths.APP_IDENTIFIER,
            )

    def test_the_directory_is_not_created_as_a_side_effect(self):
        """解決するだけで作らない（呼び出し側が書き込む直前に作る前提）。"""
        with mock.patch.object(paths.sys, "platform", "linux"), mock.patch.dict(os.environ):
            temporary = tempfile.TemporaryDirectory()
            self.addCleanup(temporary.cleanup)
            os.environ["XDG_DATA_HOME"] = temporary.name
            resolved = paths.app_data_dir()
            self.assertFalse(resolved.exists())

    def test_the_identifier_is_the_tauri_bundle_identifier(self):
        """`src-tauri/tauri.conf.json` の `identifier` とズレると保存先が2つに割れる。"""
        self.assertEqual(paths.APP_IDENTIFIER, "com.geoframe.rawassist")


class HomeResolutionFailureTests(unittest.TestCase):
    """ホーム解決の失敗を `Path.cwd()` へ逃がす（import 時に評価されるため例外を漏らせない）。

    `config.CONFIG_PATH` はモジュールレベルで `app_data_dir()` を呼ぶ予定なので、ここで
    例外が漏れると `import local_raw.config` ごと落ちてアプリが起動しなくなる。HOME 未設定・
    passwd エントリなしは稀だが実例がある（`config.display_path()` も同じガードを持つ）。
    """

    def assert_falls_back(self, platform, expected_tail, *, environ=None):
        with (
            mock.patch.object(paths.sys, "platform", platform),
            mock.patch.dict(os.environ),
            mock.patch.object(paths.Path, "home", side_effect=RuntimeError("no home")),
        ):
            for key in ("APPDATA", "XDG_DATA_HOME"):
                os.environ.pop(key, None)
            os.environ.update(environ or {})
            # 例外が伝播しないこと自体がここの主張（伝播すればこの行で落ちる）。
            resolved = paths.app_data_dir()
        self.assertEqual(resolved, Path.cwd().joinpath(*expected_tail, paths.APP_IDENTIFIER))

    def test_macos_falls_back_to_the_current_directory(self):
        self.assert_falls_back("darwin", ("Library", "Application Support"))

    def test_windows_falls_back_to_the_current_directory(self):
        self.assert_falls_back("win32", ("AppData", "Roaming"))

    def test_linux_falls_back_to_the_current_directory(self):
        self.assert_falls_back("linux", (".local", "share"))

    def test_an_oserror_from_home_is_also_caught(self):
        with (
            mock.patch.object(paths.sys, "platform", "darwin"),
            mock.patch.object(paths.Path, "home", side_effect=OSError(2, "no such file")),
        ):
            self.assertEqual(
                paths.app_data_dir(),
                Path.cwd() / "Library" / "Application Support" / paths.APP_IDENTIFIER,
            )

    def test_a_keyerror_from_home_is_also_caught(self):
        """`pwd` に該当エントリが無い環境（コンテナ等）は KeyError で現れる。"""
        with (
            mock.patch.object(paths.sys, "platform", "linux"),
            mock.patch.dict(os.environ),
            mock.patch.object(paths.Path, "home", side_effect=KeyError("getpwuid()")),
        ):
            os.environ.pop("XDG_DATA_HOME", None)
            self.assertEqual(
                paths.app_data_dir(),
                Path.cwd() / ".local" / "share" / paths.APP_IDENTIFIER,
            )

    def test_an_explicit_environment_variable_never_touches_home(self):
        """`APPDATA` / `XDG_DATA_HOME` があればホーム解決は不要（失敗しても影響しない）。"""
        for platform, key, base in (
            ("win32", "APPDATA", r"C:\Users\test\AppData\Roaming"),
            ("linux", "XDG_DATA_HOME", "/custom/share"),
        ):
            with self.subTest(platform=platform):
                with (
                    mock.patch.object(paths.sys, "platform", platform),
                    mock.patch.dict(os.environ, {key: base}, clear=False),
                    mock.patch.object(paths.Path, "home", side_effect=RuntimeError("no home")),
                ):
                    self.assertEqual(paths.app_data_dir(), Path(base) / paths.APP_IDENTIFIER)


class MigrateLegacyFileTestCase(unittest.TestCase):
    """旧パス→新パス移行の共通土台。"""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        # macOS の /var → /private/var シンボリックリンクで比較が壊れないよう resolve する。
        self.tmp = Path(temporary.name).resolve()
        self.old_dir = self.tmp / "legacy"
        self.old_dir.mkdir()
        self.old_path = self.old_dir / "config.json"
        self.new_path = self.tmp / "appdata" / "config.json"

    def write_old(self, body=b'{"source_root": "/cards/a"}'):
        self.old_path.write_bytes(body)
        return body

    def leftover_temp_files(self, directory):
        return [path.name for path in directory.glob("*.tmp*")]


class MigrationSuccessTests(MigrateLegacyFileTestCase):
    def test_the_file_is_moved_and_the_old_one_is_removed(self):
        body = self.write_old()
        self.assertTrue(paths.migrate_legacy_file(self.old_path, self.new_path))
        self.assertEqual(self.new_path.read_bytes(), body)
        self.assertFalse(self.old_path.exists())

    def test_the_parent_directory_is_created_on_demand(self):
        """初回起動時は アプリデータディレクトリがまだ存在しない。"""
        self.write_old()
        self.assertFalse(self.new_path.parent.exists())
        self.assertTrue(paths.migrate_legacy_file(self.old_path, self.new_path))
        self.assertTrue(self.new_path.is_file())

    def test_a_deeply_nested_destination_is_created(self):
        self.write_old()
        nested = self.tmp / "a" / "b" / "c" / "config.json"
        self.assertTrue(paths.migrate_legacy_file(self.old_path, nested))
        self.assertTrue(nested.is_file())

    def test_no_temporary_file_is_left_behind(self):
        """tmp + `os.replace` の一時ファイルが残っていると、次回起動時にゴミが増え続ける。"""
        self.write_old()
        paths.migrate_legacy_file(self.old_path, self.new_path)
        self.assertEqual(self.leftover_temp_files(self.new_path.parent), [])

    def test_binary_content_survives_unchanged(self):
        """テキストとして読み書きしない（トークンJSONでも壊さない）。"""
        body = self.write_old(b"\xff\xfe\x00binary\n")
        paths.migrate_legacy_file(self.old_path, self.new_path)
        self.assertEqual(self.new_path.read_bytes(), body)

    def test_an_empty_file_is_still_migrated(self):
        self.write_old(b"")
        self.assertTrue(paths.migrate_legacy_file(self.old_path, self.new_path))
        self.assertEqual(self.new_path.read_bytes(), b"")


class MigrationSkipTests(MigrateLegacyFileTestCase):
    def test_a_missing_old_path_does_nothing(self):
        self.assertFalse(self.old_path.exists())
        self.assertFalse(paths.migrate_legacy_file(self.old_path, self.new_path))
        self.assertFalse(self.new_path.exists())
        self.assertFalse(self.new_path.parent.exists())

    def test_a_directory_at_the_old_path_is_not_migrated(self):
        """ファイルでないものは移さない（`is_file()` で弾く）。"""
        self.old_path.mkdir()
        self.assertFalse(paths.migrate_legacy_file(self.old_path, self.new_path))
        self.assertFalse(self.new_path.exists())
        self.assertTrue(self.old_path.is_dir())

    def test_an_existing_new_path_is_never_overwritten(self):
        """冪等性: 2 回目以降は何もしない。旧ファイルも消さない。"""
        self.write_old(b"old body")
        self.new_path.parent.mkdir(parents=True)
        self.new_path.write_bytes(b"new body")

        self.assertFalse(paths.migrate_legacy_file(self.old_path, self.new_path))
        self.assertEqual(self.new_path.read_bytes(), b"new body")
        self.assertEqual(self.old_path.read_bytes(), b"old body", "旧ファイルを消してはいけない")

    def test_an_existing_new_path_wins_even_without_an_old_file(self):
        self.new_path.parent.mkdir(parents=True)
        self.new_path.write_bytes(b"new body")
        self.assertFalse(paths.migrate_legacy_file(self.old_path, self.new_path))
        self.assertEqual(self.new_path.read_bytes(), b"new body")

    def test_a_second_call_after_a_successful_migration_is_a_no_op(self):
        self.write_old(b"body")
        self.assertTrue(paths.migrate_legacy_file(self.old_path, self.new_path))
        # 旧パスに別の中身が復活しても、new_path があるので上書きしない。
        self.write_old(b"resurrected")
        self.assertFalse(paths.migrate_legacy_file(self.old_path, self.new_path))
        self.assertEqual(self.new_path.read_bytes(), b"body")


class MigrationPermissionTests(MigrateLegacyFileTestCase):
    def test_the_mode_is_applied_at_creation_time(self):
        """`mode` 指定時は作成時からそのパーミッション（write→chmod の窓を作らない）。

        `os.open(..., O_CREAT, mode)` の実効値は `mode & ~umask` なので、比較を
        umask に依存させないようテスト中だけ umask を 0 にする。0o077 側の検査は
        `tests/test_drive_upload.py` の `TokenPersistenceTests` と同じ「他ユーザーから
        読めないこと」を見る形。
        """
        previous = os.umask(0)
        self.addCleanup(os.umask, previous)

        self.write_old()
        self.assertTrue(paths.migrate_legacy_file(self.old_path, self.new_path, mode=0o600))
        self.assertEqual(self.new_path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.new_path.stat().st_mode & 0o077, 0, "他ユーザーから読める")

    def test_the_mode_survives_the_atomic_replace(self):
        """`os.replace` はパーミッションを持ち越すので、置換後も 0600 のまま。"""
        previous = os.umask(0o022)
        self.addCleanup(os.umask, previous)

        self.write_old()
        paths.migrate_legacy_file(self.old_path, self.new_path, mode=0o600)
        self.assertEqual(self.new_path.stat().st_mode & 0o077, 0)

    def test_without_a_mode_the_file_is_still_created(self):
        """`mode` 省略時は既定パーミッション。値そのものは umask 次第なので見ない。"""
        body = self.write_old()
        self.assertTrue(paths.migrate_legacy_file(self.old_path, self.new_path))
        self.assertEqual(self.new_path.read_bytes(), body)

    def test_no_temporary_file_is_left_behind_with_a_mode(self):
        self.write_old()
        paths.migrate_legacy_file(self.old_path, self.new_path, mode=0o600)
        self.assertEqual(self.leftover_temp_files(self.new_path.parent), [])

    @unittest.skipIf(RUNNING_AS_ROOT, "root では 0o500 でも書けるため検証できない")
    def test_a_removal_failure_still_counts_as_a_successful_migration(self):
        """旧ファイルを消せなくても True（new_path が既に正なので移行自体は完了）。

        `Path.unlink` をモックすると `finally` の一時ファイル後始末まで巻き込むので、
        旧ファイルの**親ディレクトリ**を書き込み不可にして実際に unlink を失敗させる。
        """
        body = self.write_old()
        self.old_dir.chmod(0o500)
        self.addCleanup(self.old_dir.chmod, 0o700)

        self.assertTrue(paths.migrate_legacy_file(self.old_path, self.new_path))
        self.assertEqual(self.new_path.read_bytes(), body)
        self.assertTrue(self.old_path.exists(), "この検証は旧ファイルが残る状況が前提")

    @unittest.skipIf(RUNNING_AS_ROOT, "root では 0o000 でも読めるため検証できない")
    def test_an_unreadable_old_file_is_reported_as_a_failure(self):
        self.write_old()
        self.old_path.chmod(0o000)
        self.addCleanup(self.old_path.chmod, 0o600)

        self.assertFalse(paths.migrate_legacy_file(self.old_path, self.new_path))
        self.assertFalse(self.new_path.exists())


class MigrationNeverRaisesTests(MigrateLegacyFileTestCase):
    """起動経路から呼ばれるので、どんな壊れ方でも例外を投げずに False を返すこと。"""

    def test_an_unbuildable_parent_directory_returns_false(self):
        """親に**普通のファイル**が居座っていて mkdir が ENOTDIR になるケース。

        `mkdir` の失敗で一時ファイル名が未束縛のまま後始末に入ると NameError（OSError では
        ないので呼び出し側へ漏れる）になる。ここはその退行検出。
        """
        self.write_old()
        blocker = self.tmp / "blocker"
        blocker.write_bytes(b"not a directory")
        target = blocker / "sub" / "config.json"

        self.assertFalse(paths.migrate_legacy_file(self.old_path, target))
        self.assertEqual(blocker.read_bytes(), b"not a directory", "邪魔者を壊してはいけない")
        self.assertTrue(self.old_path.exists(), "失敗時に旧ファイルを消してはいけない")

    def test_an_unbuildable_parent_directory_returns_false_with_a_mode(self):
        self.write_old()
        blocker = self.tmp / "blocker"
        blocker.write_bytes(b"not a directory")
        target = blocker / "sub" / "drive_auth.json"

        self.assertFalse(paths.migrate_legacy_file(self.old_path, target, mode=0o600))
        self.assertTrue(self.old_path.exists())

    @unittest.skipIf(RUNNING_AS_ROOT, "root では読み取り専用ディレクトリにも書けるため検証できない")
    def test_an_unwritable_destination_directory_returns_false(self):
        self.write_old()
        locked = self.tmp / "locked"
        locked.mkdir()
        locked.chmod(0o500)
        self.addCleanup(locked.chmod, 0o700)
        target = locked / "config.json"

        self.assertFalse(paths.migrate_legacy_file(self.old_path, target))
        self.assertFalse(target.exists())
        self.assertTrue(self.old_path.exists(), "移せていないのに旧ファイルを消してはいけない")

    def test_a_replace_failure_cleans_up_the_temporary_file(self):
        """`os.replace` が落ちても一時ファイルを残さず False を返す。"""
        self.write_old()

        def failing_replace(src, dst):
            raise OSError(5, "Input/output error")

        with mock.patch.object(os, "replace", failing_replace):
            self.assertFalse(paths.migrate_legacy_file(self.old_path, self.new_path))

        self.assertFalse(self.new_path.exists())
        self.assertEqual(self.leftover_temp_files(self.new_path.parent), [])
        self.assertTrue(self.old_path.exists())


class TemporaryFileNamingTests(MigrateLegacyFileTestCase):
    """一時ファイル名は `with_name` ベース（`with_suffix` は拡張子を丸ごと置換する）。"""

    def test_the_temporary_name_keeps_the_full_destination_name(self):
        self.write_old()
        captured: list[Path] = []
        original_replace = os.replace

        def capturing_replace(src, dst):
            captured.append(Path(src))
            return original_replace(src, dst)

        with mock.patch.object(os, "replace", capturing_replace):
            paths.migrate_legacy_file(self.old_path, self.new_path)

        self.assertEqual(len(captured), 1)
        temporary = captured[0]
        self.assertEqual(temporary.parent, self.new_path.parent, "同じディレクトリに作る")
        self.assertTrue(
            temporary.name.startswith(f"{self.new_path.name}."),
            f"元の名前 {self.new_path.name} を保っていない: {temporary.name}",
        )
        self.assertTrue(temporary.name.endswith(".tmp"))

    def test_temporary_names_are_unique_across_calls(self):
        captured: list[Path] = []
        original_replace = os.replace

        def capturing_replace(src, dst):
            captured.append(Path(src))
            return original_replace(src, dst)

        with mock.patch.object(os, "replace", capturing_replace):
            for index in range(3):
                self.write_old(f"body-{index}".encode())
                target = self.tmp / "appdata" / f"config-{index}.json"
                paths.migrate_legacy_file(self.old_path, target)

        self.assertEqual(len(set(captured)), 3, "呼び出しごとに別の一時ファイル名になるべき")


if __name__ == "__main__":
    unittest.main()
