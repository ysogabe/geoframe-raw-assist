"""現像元フォルダ設定（`local_raw.config`）の検証。

実ファイルは合成データのみ。`CONFIG_PATH` は必ず tmpdir に差し替えて、
リポジトリ内の `outputs/config.json` に触らないようにする。
"""

import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from local_raw import config
from local_raw.config import SourceFolderError

#: root で走らせるとパーミッション検査が成立しない（root は 0o000 でも読めてしまう）。
RUNNING_AS_ROOT = hasattr(os, "geteuid") and os.geteuid() == 0


class ConfigTestCase(unittest.TestCase):
    """tmpdir に設定ファイルとフォルダ群を用意する共通土台。"""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        # macOS の /var → /private/var シンボリックリンクで比較が壊れないよう resolve する。
        self.tmp = Path(temporary.name).resolve()
        self.config_path = self.tmp / "outputs" / "config.json"
        patcher = mock.patch.object(config, "CONFIG_PATH", self.config_path)
        patcher.start()
        self.addCleanup(patcher.stop)

    def make_folder(self, name, *files):
        folder = self.tmp / name
        folder.mkdir(parents=True, exist_ok=True)
        for filename in files:
            (folder / filename).write_bytes(b"x" * 10)
        return folder

    def stored_config(self):
        return json.loads(self.config_path.read_text(encoding="utf-8"))


class SetAndReadTests(ConfigTestCase):
    def test_round_trip(self):
        folder = self.make_folder("card-a")
        self.assertEqual(config.set_source_root(str(folder)), folder)
        self.assertEqual(config.source_root(), folder)

    def test_stored_value_is_the_resolved_absolute_path(self):
        """保存は解決済みパスに固定する（root_key と params_hash の正規化を1点に集約するため）。"""
        folder = self.make_folder("card-a")
        link = self.tmp / "link-to-card-a"
        link.symlink_to(folder, target_is_directory=True)

        config.set_source_root(str(link))
        self.assertEqual(self.stored_config()[config.SOURCE_ROOT_KEY], str(folder))
        self.assertEqual(config.source_root(), folder)

    def test_config_file_shape(self):
        folder = self.make_folder("card-a")
        config.set_source_root(str(folder))
        stored = self.stored_config()
        self.assertEqual(stored["config_version"], config.CONFIG_VERSION)
        self.assertEqual(stored[config.SOURCE_ROOT_KEY], str(folder))

    def test_write_is_atomic_and_leaves_no_temporary_file(self):
        folder = self.make_folder("card-a")
        config.set_source_root(str(folder))
        self.assertEqual(list(self.config_path.parent.glob("*.tmp.json")), [])
        self.assertTrue(self.config_path.is_file())

    def test_overwrites_a_previous_setting(self):
        first = self.make_folder("card-a")
        second = self.make_folder("card-b")
        config.set_source_root(str(first))
        config.set_source_root(str(second))
        self.assertEqual(config.source_root(), second)


class WriteConfigConcurrencyTests(ConfigTestCase):
    """修正: _write_config の一時ファイル名が同時実行で衝突しない（engine._temp_path と同方式）。

    ThreadingHTTPServer はリクエストごとに短命スレッドを作る。旧実装は `pid` と
    `threading.get_ident()` だけで一時ファイル名を決めており、CPython はスレッド終了後に
    識別子を再利用するため、連続リクエストで同名の一時ファイルに書き込みが衝突し得た。
    今は呼び出しごとに `uuid4()` を振るので、別スレッドから同時に呼ばれても全て別パスになる。
    """

    def test_temp_names_differ_across_concurrent_threads(self):
        """複数スレッドから同時に _write_config を呼んでも一時ファイル名は全て別々。

        `os.replace` に渡される src（= 一時ファイルパス）を捕まえて、全スレッドで別々の
        パスが使われたことを検証する。書き込み自体はアトミックなので最終的な config.json は
        どれか1回分の結果が残り、一時ファイルは残らない。
        """
        folder = self.make_folder("card-a")
        payload = {
            "config_version": config.CONFIG_VERSION,
            config.SOURCE_ROOT_KEY: str(folder),
        }
        captured: list[Path] = []
        lock = threading.Lock()
        original_replace = os.replace
        barrier = threading.Barrier(4)

        def capturing_replace(src, dst):
            with lock:
                captured.append(Path(src))
            return original_replace(src, dst)

        def worker():
            barrier.wait()
            config._write_config(payload)

        with mock.patch.object(os, "replace", capturing_replace):
            threads = [threading.Thread(target=worker) for _ in range(4)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()

        self.assertEqual(len(captured), 4, "4スレッド全てが書き込みを完了する")
        self.assertEqual(
            len(set(captured)),
            4,
            "同時に呼ばれた別スレッドの一時ファイル名は全て別々であるべき",
        )
        self.assertTrue(self.config_path.is_file())
        self.assertEqual(config.source_root(), folder)
        self.assertEqual(list(self.config_path.parent.glob("*.tmp.json")), [])


class FallbackTests(ConfigTestCase):
    def default(self):
        return config.DEFAULT_SOURCE_ROOT.resolve()

    def test_missing_config_file(self):
        self.assertFalse(self.config_path.exists())
        self.assertEqual(config.source_root(), self.default())

    def test_broken_json(self):
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        self.config_path.write_text("{ not json", encoding="utf-8")
        self.assertEqual(config.source_root(), self.default())

    def test_non_dict_json(self):
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        self.config_path.write_text('["a"]', encoding="utf-8")
        self.assertEqual(config.source_root(), self.default())

    def test_stored_folder_no_longer_exists(self):
        folder = self.make_folder("card-a")
        config.set_source_root(str(folder))
        folder.rmdir()
        self.assertEqual(config.source_root(), self.default())

    def test_stored_value_is_a_file_not_a_directory(self):
        target = self.tmp / "not-a-folder.txt"
        target.write_text("x", encoding="utf-8")
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        self.config_path.write_text(
            json.dumps({config.SOURCE_ROOT_KEY: str(target)}), encoding="utf-8"
        )
        self.assertEqual(config.source_root(), self.default())

    def test_stored_value_is_relative_or_not_a_string(self):
        for stored in ("relative/path", "", 42, None, []):
            with self.subTest(stored=stored):
                self.config_path.parent.mkdir(parents=True, exist_ok=True)
                self.config_path.write_text(
                    json.dumps({config.SOURCE_ROOT_KEY: stored}), encoding="utf-8"
                )
                self.assertEqual(config.source_root(), self.default())

    def test_unreadable_folder_is_not_replaced_by_the_default(self):
        """「読めないだけ」のフォルダは既定へフォールバックしない（docstring の約束）。

        既定に戻してしまうと、UI は「許可すれば直る」ことを案内できず、ユーザーには
        選んだフォルダが黙って無かったことにされたように見える。macOS の TCC 拒否は
        `os.stat` が `PermissionError` になる形で現れるので、その形で再現する。
        `os.stat` はモジュール全体で共有されているため、対象フォルダ以外は本物へ
        委譲する（tempfile の後始末まで巻き込まないため）。
        """
        folder = self.make_folder("card-a")
        config.set_source_root(str(folder))
        real_stat = os.stat

        def denied(path, *args, **kwargs):
            if Path(path) == folder:
                raise PermissionError(13, "Permission denied")
            return real_stat(path, *args, **kwargs)

        with mock.patch.object(os, "stat", denied):
            resolved = config.source_root()

        self.assertEqual(resolved, folder)
        self.assertNotEqual(resolved, self.default())

    def test_never_raises(self):
        """UI から常に呼ばれるので、どんな壊れ方でも例外を投げないこと。"""
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        self.config_path.write_bytes(b"\xff\xfe\x00binary")
        self.assertIsInstance(config.source_root(), Path)


class ValidationTests(ConfigTestCase):
    def expect_reason(self, path, reason, **kwargs):
        with self.assertRaises(SourceFolderError) as caught:
            config.set_source_root(path, **kwargs)
        self.assertEqual(caught.exception.reason, reason)
        self.assertTrue(str(caught.exception), "日本語メッセージが空")
        return caught.exception

    def test_invalid_types_and_empty_values(self):
        for path in (None, 42, [], {}, "", "   "):
            with self.subTest(path=path):
                self.expect_reason(path, "invalid")

    def test_relative_path_is_invalid(self):
        self.expect_reason("sample/100MSDCF", "invalid")

    def test_nul_byte_is_invalid_not_an_unclassified_error(self):
        """`os.stat` が `ValueError` を投げる経路。理由なしの 400 を作らないこと。"""
        self.expect_reason("/tmp/bad\x00path", "invalid")

    def test_over_long_path_is_invalid(self):
        self.expect_reason("/" + ("a" * 4096), "invalid")

    def test_missing_folder_is_not_found(self):
        self.expect_reason(str(self.tmp / "no-such-folder"), "not_found")

    def test_file_is_not_directory(self):
        target = self.tmp / "photo.JPG"
        target.write_bytes(b"x")
        self.expect_reason(str(target), "not_directory")

    @unittest.skipIf(RUNNING_AS_ROOT, "root では 0o000 でも読めるため検証できない")
    def test_unreadable_folder_is_permission_denied(self):
        folder = self.make_folder("locked")
        folder.chmod(0o000)
        self.addCleanup(folder.chmod, 0o700)
        error = self.expect_reason(str(folder), "permission_denied")
        self.assertIn("プライバシーとセキュリティ", str(error))

    @unittest.skipIf(RUNNING_AS_ROOT, "root では 0o000 でも読めるため検証できない")
    def test_permission_denied_does_not_save(self):
        good = self.make_folder("card-a")
        config.set_source_root(str(good))
        locked = self.make_folder("locked")
        locked.chmod(0o000)
        self.addCleanup(locked.chmod, 0o700)
        self.expect_reason(str(locked), "permission_denied")
        self.assertEqual(config.source_root(), good)

    def test_analysis_running_is_rejected_and_not_saved(self):
        good = self.make_folder("card-a")
        config.set_source_root(str(good))
        other = self.make_folder("card-b")
        self.expect_reason(str(other), "analysis_running", analysis_running=True)
        self.assertEqual(config.source_root(), good)

    def test_failed_validation_does_not_create_a_config_file(self):
        self.expect_reason(str(self.tmp / "no-such-folder"), "not_found")
        self.assertFalse(self.config_path.exists())

    def test_empty_folder_is_accepted(self):
        """読み取れて 0 組なら、後からファイルを置くケースがあるのでエラーにしない。"""
        folder = self.make_folder("empty-card")
        self.assertEqual(config.set_source_root(str(folder)), folder)

    def test_error_is_a_value_error(self):
        """既存の POST 例外マッピング（ValueError → 400）に乗ること。"""
        self.assertTrue(issubclass(SourceFolderError, ValueError))


class RootKeyTests(ConfigTestCase):
    def test_length_and_stability(self):
        folder = self.make_folder("card-a")
        key = config.root_key_for(folder)
        self.assertEqual(len(key), config.ROOT_KEY_LENGTH)
        self.assertEqual(key, config.root_key_for(folder))

    def test_differs_between_folders(self):
        self.assertNotEqual(
            config.root_key_for(self.make_folder("card-a")),
            config.root_key_for(self.make_folder("card-b")),
        )

    def test_is_resolved_so_aliases_share_one_key(self):
        """シンボリックリンク経由でも同じキー（キャッシュが半分だけ無効化されない）。"""
        folder = self.make_folder("card-a")
        link = self.tmp / "alias"
        link.symlink_to(folder, target_is_directory=True)
        self.assertEqual(config.root_key_for(folder), config.root_key_for(link))

    def test_source_root_key_follows_the_setting(self):
        first = self.make_folder("card-a")
        config.set_source_root(str(first))
        self.assertEqual(config.source_root_key(), config.root_key_for(first))
        second = self.make_folder("card-b")
        config.set_source_root(str(second))
        self.assertEqual(config.source_root_key(), config.root_key_for(second))


class DisplayPathTests(ConfigTestCase):
    def test_inside_the_project_is_project_relative(self):
        self.assertEqual(
            config.display_path(config.PROJECT_ROOT / "sample" / "100MSDCF"),
            "sample/100MSDCF",
        )

    def test_inside_the_home_directory_is_tilde(self):
        self.assertEqual(
            config.display_path(Path.home() / "Pictures" / "geoframe"),
            "~/Pictures/geoframe",
        )

    def test_home_itself(self):
        self.assertEqual(config.display_path(Path.home()), "~")

    def test_outside_both_is_absolute(self):
        self.assertEqual(
            config.display_path(Path("/Volumes/CARD-A/DCIM/100MSDCF")),
            "/Volumes/CARD-A/DCIM/100MSDCF",
        )

    def test_project_wins_over_home(self):
        """プロジェクト自体がホーム配下にあるので、判定順を入れ替えると `~/...` に化ける。"""
        self.assertFalse(config.display_path(config.PROJECT_ROOT / "sample").startswith("~"))


class SourceFolderInfoTests(ConfigTestCase):
    def test_counts_come_from_scan_pairs(self):
        folder = self.make_folder(
            "card-a", "DSC00001.JPG", "DSC00001.ARW", "DSC00002.JPG", "DSC00003.ARW"
        )
        info = config.source_folder_info(folder)
        self.assertEqual(info["pairs"], 1)
        self.assertEqual(info["jpg_only"], 1)
        self.assertEqual(info["arw_only"], 1)
        self.assertEqual(info["total_bytes"], 40)

    def test_shape_for_a_normal_folder(self):
        folder = self.make_folder("card-a", "DSC00001.JPG", "DSC00001.ARW")
        info = config.source_folder_info(folder)
        self.assertEqual(info["path"], str(folder))
        self.assertEqual(info["display_path"], config.display_path(folder))
        self.assertFalse(info["is_default"])
        self.assertTrue(info["exists"])
        self.assertTrue(info["readable"])

    def test_follows_the_saved_setting_when_called_without_an_argument(self):
        folder = self.make_folder("card-a", "DSC00001.JPG", "DSC00001.ARW")
        config.set_source_root(str(folder))
        self.assertEqual(config.source_folder_info()["path"], str(folder))

    def test_default_folder_is_flagged(self):
        info = config.source_folder_info(config.DEFAULT_SOURCE_ROOT)
        self.assertTrue(info["is_default"])
        self.assertEqual(info["display_path"], "sample/100MSDCF")

    def test_missing_folder_reports_zero_without_raising(self):
        info = config.source_folder_info(self.tmp / "no-such-folder")
        self.assertFalse(info["exists"])
        self.assertFalse(info["readable"])
        self.assertEqual(info["pairs"], 0)
        self.assertEqual(info["total_bytes"], 0)

    def test_empty_folder_exists_and_is_readable(self):
        info = config.source_folder_info(self.make_folder("empty-card"))
        self.assertTrue(info["exists"])
        self.assertTrue(info["readable"])
        self.assertEqual(info["pairs"], 0)

    @unittest.skipIf(RUNNING_AS_ROOT, "root では 0o000 でも読めるため検証できない")
    def test_unreadable_folder_is_not_confused_with_an_empty_one(self):
        """`Path.glob` が PermissionError を握り潰すので、0組と読めないは別に見えないといけない。"""
        folder = self.make_folder("locked", "DSC00001.JPG", "DSC00001.ARW")
        folder.chmod(0o000)
        self.addCleanup(folder.chmod, 0o700)
        info = config.source_folder_info(folder)
        self.assertTrue(info["exists"])
        self.assertFalse(info["readable"])
        self.assertEqual(info["pairs"], 0)


if __name__ == "__main__":
    unittest.main()
