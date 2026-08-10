-- GeoFrame RAW Assist: 書き出し済みJPEGを Apple「写真」のアルバムへ取り込む。
--
--   osascript scripts/photos_import.applescript "<アルバム名>" "<ファイル絶対パス>" ...
--
-- 引数（argv）としてのみ受け取り、スクリプトを文字列連結で組み立てない（契約 §1）。
-- 同名アルバムが無ければ作成し、指定ファイルをそのアルバムへ import する。
-- 標準出力には取り込めた枚数だけを返す。

on run argv
	if (count of argv) < 2 then
		error "アルバム名と1つ以上のファイルパスを指定してください。" number 1
	end if

	set albumName to item 1 of argv
	set importFiles to {}
	repeat with argIndex from 2 to (count of argv)
		set end of importFiles to (POSIX file (item argIndex of argv)) as alias
	end repeat

	set importedCount to 0
	tell application "Photos"
		with timeout of 170 seconds
			set targetAlbum to missing value
			try
				set targetAlbum to album albumName
			end try
			if targetAlbum is missing value then
				set targetAlbum to make new album named albumName
			end if

			set importedItems to import importFiles into targetAlbum
			try
				set importedCount to count of importedItems
			end try
		end timeout
	end tell

	return importedCount as text
end run
