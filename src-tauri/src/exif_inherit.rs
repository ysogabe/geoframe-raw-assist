//! EXIF APP1 の生コピー。GPS を含む既存領域を再シリアライズしない。
//! オフセットはすべて TIFF ヘッダ基準。変更対象が欠落／不正なら継承を断念する。
use std::collections::HashSet;
use std::ops::Range;

const EXIF: &[u8] = b"Exif\0\0";
const MAX_APP1: usize = 65533;

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ExifInheritError {
    InvalidJpeg,
    MissingExif,
    InvalidTiff,
    MissingRequiredTag,
    DimensionOverflow,
    InvalidSoftware,
    App1TooLarge,
}
impl std::fmt::Display for ExifInheritError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "EXIF inheritance failed: {self:?}")
    }
}
impl std::error::Error for ExifInheritError {}
type Result<T> = std::result::Result<T, ExifInheritError>;
use ExifInheritError::*;

#[derive(Clone, Copy)]
struct Endian(bool);
impl Endian {
    fn u16(self, b: &[u8], p: usize) -> Result<u16> {
        let a = b.get(p..p + 2).ok_or(InvalidTiff)?.try_into().unwrap();
        Ok(if self.0 {
            u16::from_le_bytes(a)
        } else {
            u16::from_be_bytes(a)
        })
    }
    fn u32(self, b: &[u8], p: usize) -> Result<u32> {
        let a = b.get(p..p + 4).ok_or(InvalidTiff)?.try_into().unwrap();
        Ok(if self.0 {
            u32::from_le_bytes(a)
        } else {
            u32::from_be_bytes(a)
        })
    }
    fn put16(self, b: &mut [u8], p: usize, v: u16) {
        b[p..p + 2].copy_from_slice(&if self.0 {
            v.to_le_bytes()
        } else {
            v.to_be_bytes()
        });
    }
    fn put32(self, b: &mut [u8], p: usize, v: u32) {
        b[p..p + 4].copy_from_slice(&if self.0 {
            v.to_le_bytes()
        } else {
            v.to_be_bytes()
        });
    }
}

struct Segment {
    marker: u8,
    range: Range<usize>,
    payload: Range<usize>,
}
// エントロピー符号・restart・複数 scan も保持し、scan 間の APP1 も除去対象にする。
fn segments(jpeg: &[u8]) -> Result<Vec<Segment>> {
    if !jpeg.starts_with(&[0xff, 0xd8]) {
        return Err(InvalidJpeg);
    }
    let mut p = 2;
    let mut result = Vec::new();
    while p < jpeg.len() {
        let start = p;
        if jpeg[p] != 0xff {
            return Err(InvalidJpeg);
        }
        while jpeg.get(p) == Some(&0xff) {
            p += 1;
        }
        let marker = *jpeg.get(p).ok_or(InvalidJpeg)?;
        p += 1;
        if marker == 0xd9 {
            result.push(Segment {
                marker,
                range: start..jpeg.len(),
                payload: p..p,
            });
            return Ok(result);
        }
        if marker == 0 || marker == 0xd8 || (0xd0..=0xd7).contains(&marker) {
            return Err(InvalidJpeg);
        }
        if marker == 1 {
            result.push(Segment {
                marker,
                range: start..p,
                payload: p..p,
            });
            continue;
        }
        let len = jpeg.get(p..p + 2).ok_or(InvalidJpeg)?;
        let len = u16::from_be_bytes([len[0], len[1]]) as usize;
        if len < 2 || p + len > jpeg.len() {
            return Err(InvalidJpeg);
        }
        let payload = p + 2..p + len;
        p += len;
        if marker == 0xda {
            loop {
                if p >= jpeg.len() {
                    return Err(InvalidJpeg);
                }
                if jpeg[p] != 0xff {
                    p += 1;
                    continue;
                }
                let mut q = p + 1;
                while jpeg.get(q) == Some(&0xff) {
                    q += 1;
                }
                match jpeg.get(q).ok_or(InvalidJpeg)? {
                    0 | 0xd0..=0xd7 => p = q + 1,
                    _ => break,
                }
            }
        }
        result.push(Segment {
            marker,
            range: start..p,
            payload,
        });
    }
    Err(InvalidJpeg)
}

fn exif_payload(jpeg: &[u8]) -> Result<&[u8]> {
    let mut found = None;
    for s in segments(jpeg)? {
        let payload = &jpeg[s.payload];
        if s.marker == 0xe1 && payload.starts_with(EXIF) {
            if found.is_some() {
                return Err(InvalidTiff);
            }
            found = Some(payload);
        }
    }
    found.ok_or(MissingExif)
}

#[derive(Clone)]
struct Ifd {
    start: usize,
    entries: Vec<usize>,
    next: usize,
}
fn ifd(b: &[u8], e: Endian, p: usize) -> Result<Ifd> {
    if p < 8 {
        return Err(InvalidTiff);
    }
    let n = e.u16(b, p)? as usize;
    let next = p + 2 + n * 12;
    e.u32(b, next)?;
    Ok(Ifd {
        start: p,
        entries: (0..n).map(|i| p + 2 + 12 * i).collect(),
        next,
    })
}
fn tag(b: &[u8], e: Endian, d: &Ifd, id: u16) -> Result<Option<usize>> {
    let mut found = None;
    for &p in &d.entries {
        if e.u16(b, p)? == id {
            if found.is_some() {
                return Err(InvalidTiff);
            }
            found = Some(p);
        }
    }
    Ok(found)
}
fn required(b: &[u8], e: Endian, d: &Ifd, id: u16) -> Result<usize> {
    tag(b, e, d, id)?.ok_or(MissingRequiredTag)
}
fn pointer(b: &[u8], e: Endian, p: usize) -> Result<usize> {
    if e.u16(b, p + 2)? != 4 || e.u32(b, p + 4)? != 1 {
        return Err(InvalidTiff);
    }
    Ok(e.u32(b, p + 8)? as usize)
}
fn data_range(b: &[u8], e: Endian, p: usize) -> Result<Range<usize>> {
    let unit = match e.u16(b, p + 2)? {
        1 | 2 | 6 | 7 => 1,
        3 | 8 => 2,
        4 | 9 | 11 | 13 => 4,
        5 | 10 | 12 => 8,
        _ => return Err(InvalidTiff),
    };
    let n = (e.u32(b, p + 4)? as usize)
        .checked_mul(unit)
        .ok_or(InvalidTiff)?;
    let start = if n <= 4 {
        p + 8
    } else {
        e.u32(b, p + 8)? as usize
    };
    let end = start.checked_add(n).ok_or(InvalidTiff)?;
    b.get(start..end).ok_or(InvalidTiff)?;
    Ok(start..end)
}
// 全 IFD と外部値の境界・循環・重なりを検証。書換えが GPS 等の別領域に波及する
// alias を拒否する。MakerNote / PrintImageMatching の私用値は opaque のまま扱う。
fn validate(b: &[u8], e: Endian, root: usize) -> Result<()> {
    let mut pending = vec![root];
    let mut seen = HashSet::new();
    let mut regions = vec![0..8];
    while let Some(p) = pending.pop() {
        if !seen.insert(p) {
            return Err(InvalidTiff);
        }
        let d = ifd(b, e, p)?;
        regions.push(p..d.next + 4);
        let mut tags = HashSet::new();
        for &q in &d.entries {
            let id = e.u16(b, q)?;
            if !tags.insert(id) {
                return Err(InvalidTiff);
            }
            let r = data_range(b, e, q)?;
            if r.len() > 4 {
                regions.push(r);
            }
            if matches!(id, 0x8769 | 0x8825 | 0xa005) {
                let child = pointer(b, e, q)?;
                if child == 0 {
                    return Err(InvalidTiff);
                }
                pending.push(child);
            }
        }
        let next = e.u32(b, d.next)? as usize;
        if next != 0 {
            pending.push(next);
        }
    }
    regions.sort_by_key(|r| r.start);
    if regions.windows(2).any(|r| r[0].end > r[1].start) {
        return Err(InvalidTiff);
    }
    Ok(())
}
fn scalar(b: &mut [u8], e: Endian, p: usize, v: u32) -> Result<()> {
    if e.u32(b, p + 4)? != 1 {
        return Err(InvalidTiff);
    }
    match e.u16(b, p + 2)? {
        3 => e.put16(b, p + 8, u16::try_from(v).map_err(|_| DimensionOverflow)?),
        4 => e.put32(b, p + 8, v),
        _ => return Err(InvalidTiff),
    }
    Ok(())
}
fn append(b: &mut Vec<u8>, bytes: &[u8]) -> Result<usize> {
    let p = (b.len() + 1) & !1; // TIFF word alignment
    if p + bytes.len() + EXIF.len() > MAX_APP1 {
        return Err(App1TooLarge);
    }
    b.resize(p, 0);
    b.extend_from_slice(bytes);
    Ok(p)
}
fn rewrite(payload: &[u8], out_w: u32, out_h: u32, software: &str) -> Result<Vec<u8>> {
    if out_w == 0 || out_h == 0 {
        return Err(DimensionOverflow);
    }
    if !software.is_ascii() || software.as_bytes().contains(&0) {
        return Err(InvalidSoftware);
    }
    if software.len() + 1 > MAX_APP1 {
        return Err(App1TooLarge);
    }
    let mut b = payload.get(EXIF.len()..).ok_or(InvalidTiff)?.to_vec();
    let e = match b.get(..2) {
        Some(b"II") => Endian(true),
        Some(b"MM") => Endian(false),
        _ => return Err(InvalidTiff),
    };
    if e.u16(&b, 2)? != 42 {
        return Err(InvalidTiff);
    }
    let root = e.u32(&b, 4)? as usize;
    validate(&b, e, root)?;
    let d = ifd(&b, e, root)?;
    let orientation = required(&b, e, &d, 0x112)?;
    if e.u16(&b, orientation + 2)? != 3 {
        return Err(InvalidTiff);
    }
    scalar(&mut b, e, orientation, 1)?;
    let exif = pointer(&b, e, required(&b, e, &d, 0x8769)?)?;
    let x = ifd(&b, e, exif)?;
    for (id, v) in [(0xa002, out_w), (0xa003, out_h)] {
        let p = required(&b, e, &x, id)?;
        scalar(&mut b, e, p, v)?;
    }
    if let Some(p) = tag(&b, e, &x, 0x927c)? {
        // 次 IFD ポインタまで一緒に詰める。参照されなくなった末尾は元のまま。
        b.copy_within(p + 12..x.next + 4, p);
        e.put16(&mut b, x.start, x.entries.len() as u16 - 1);
    }
    e.put32(&mut b, d.next, 0); // IFD1 を参照から外す
    let mut text = software.as_bytes().to_vec();
    text.push(0);
    if let Some(p) = tag(&b, e, &d, 0x131)? {
        if e.u16(&b, p + 2)? != 2 {
            return Err(InvalidTiff);
        }
        let old = data_range(&b, e, p)?;
        let pos = if text.len() <= 4 {
            p + 8
        } else if text.len() <= old.len() {
            old.start
        } else {
            append(&mut b, &text)?
        };
        b[pos..pos + text.len()].copy_from_slice(&text);
        e.put32(&mut b, p + 4, text.len() as u32);
        if text.len() > 4 {
            e.put32(&mut b, p + 8, pos as u32);
        }
    } else {
        let mut entry = vec![0; 12];
        e.put16(&mut entry, 0, 0x131);
        e.put16(&mut entry, 2, 2);
        e.put32(&mut entry, 4, text.len() as u32);
        if text.len() <= 4 {
            entry[8..8 + text.len()].copy_from_slice(&text);
        } else {
            let pos = append(&mut b, &text)?;
            e.put32(&mut entry, 8, pos as u32);
        }
        let mut entries: Vec<Vec<u8>> = d.entries.iter().map(|&p| b[p..p + 12].to_vec()).collect();
        entries.push(entry);
        entries.sort_by_key(|v| e.u16(v, 0).unwrap());
        let n = u16::try_from(entries.len()).map_err(|_| App1TooLarge)?;
        let mut relocated = vec![0; 2];
        e.put16(&mut relocated, 0, n);
        for v in entries {
            relocated.extend_from_slice(&v);
        }
        relocated.extend_from_slice(&[0; 4]);
        let pos = append(&mut b, &relocated)?;
        e.put32(&mut b, 4, pos as u32);
    }
    let mut result = EXIF.to_vec();
    result.extend_from_slice(&b);
    if result.len() > MAX_APP1 {
        return Err(App1TooLarge);
    }
    Ok(result)
}

/// ペア JPG の APP1 を保持し、正立・実寸・Software・MakerNote/IFD1 の参照だけを変更。
/// SHORT の寸法が u16 に収まらない場合や必須タグ欠落は Err（呼出側は EXIF 無しで続行）。
pub fn inherit_exif(
    developed_jpeg: &[u8],
    source_jpeg: &[u8],
    out_w: u32,
    out_h: u32,
    software: &str,
) -> Result<Vec<u8>> {
    let payload = rewrite(exif_payload(source_jpeg)?, out_w, out_h, software)?;
    let mut out = vec![0xff, 0xd8, 0xff, 0xe1];
    out.extend_from_slice(&((payload.len() + 2) as u16).to_be_bytes());
    out.extend_from_slice(&payload);
    for s in segments(developed_jpeg)? {
        if s.marker == 0xe1 && developed_jpeg[s.payload.clone()].starts_with(EXIF) {
            continue;
        }
        out.extend_from_slice(&developed_jpeg[s.range]);
    }
    Ok(out)
}

#[cfg(test)]
pub(crate) mod tests {
    use super::*;
    use std::io::Cursor;

    fn entry(b: &mut [u8], e: Endian, p: usize, id: u16, kind: u16, count: u32, value: u32) {
        e.put16(b, p, id);
        e.put16(b, p + 2, kind);
        e.put32(b, p + 4, count);
        if kind == 3 && count == 1 {
            e.put16(b, p + 8, value as u16);
        } else {
            e.put32(b, p + 8, value);
        }
    }
    pub(crate) fn fixture(little: bool, short: bool, software_capacity: Option<usize>) -> Vec<u8> {
        let e = Endian(little);
        let mut b = vec![0; 512];
        b[..2].copy_from_slice(if little { b"II" } else { b"MM" });
        e.put16(&mut b, 2, 42);
        e.put32(&mut b, 4, 8);
        e.put16(&mut b, 8, 5);
        entry(&mut b, e, 10, 0x112, 3, 1, 8);
        entry(
            &mut b,
            e,
            22,
            if software_capacity.is_some() {
                0x131
            } else {
                0x132
            },
            2,
            software_capacity.unwrap_or(8) as u32,
            432,
        );
        if software_capacity.is_some_and(|n| n <= 4) {
            b[30..34].copy_from_slice(b"old\0");
        } else {
            b[432..436].copy_from_slice(b"old\0");
        }
        entry(&mut b, e, 34, 0x8769, 4, 1, 100);
        entry(&mut b, e, 46, 0x8825, 4, 1, 200);
        entry(&mut b, e, 58, 0xc4a5, 7, 8, 416);
        e.put32(&mut b, 70, 280);
        e.put16(&mut b, 100, 5);
        entry(&mut b, e, 102, 0x927c, 7, 8, 340);
        entry(&mut b, e, 114, 0x9286, 7, 8, 348);
        let kind = if short { 3 } else { 4 };
        entry(&mut b, e, 126, 0xa002, kind, 1, 6000);
        entry(&mut b, e, 138, 0xa003, kind, 1, 4000);
        entry(&mut b, e, 150, 0xa005, 4, 1, 300);
        e.put32(&mut b, 162, 0);
        e.put16(&mut b, 200, 5);
        entry(&mut b, e, 202, 0, 1, 4, 0);
        entry(&mut b, e, 214, 1, 2, 2, 0);
        b[222..224].copy_from_slice(b"N\0");
        entry(&mut b, e, 226, 2, 5, 3, 360);
        entry(&mut b, e, 238, 3, 2, 2, 0);
        b[246..248].copy_from_slice(b"E\0");
        entry(&mut b, e, 250, 4, 5, 3, 384);
        // 架空 GPS = (0, 0)。有理数の分母のみ 1。
        for p in (364..408).step_by(8) {
            e.put32(&mut b, p, 1);
        }
        e.put16(&mut b, 280, 1);
        entry(&mut b, e, 282, 0x103, 3, 1, 6);
        e.put16(&mut b, 300, 1);
        entry(&mut b, e, 302, 1, 2, 4, 0);
        b[310..314].copy_from_slice(b"R98\0");
        b[340..348].copy_from_slice(b"MAKER123");
        b[348..356].copy_from_slice(b"ASCII\0\0\0");
        b[416..424].copy_from_slice(b"PrintIM\0");
        let mut payload = EXIF.to_vec();
        payload.extend_from_slice(&b);
        jpeg(&[(0xe1, &payload)])
    }
    pub(crate) fn jpeg(parts: &[(u8, &[u8])]) -> Vec<u8> {
        let mut b = vec![0xff, 0xd8];
        for (marker, payload) in parts {
            b.extend_from_slice(&[0xff, *marker]);
            b.extend_from_slice(&((payload.len() + 2) as u16).to_be_bytes());
            b.extend_from_slice(payload);
        }
        b.extend_from_slice(&[0xff, 0xd9]);
        b
    }
    fn tiff(jpeg: &[u8]) -> &[u8] {
        &exif_payload(jpeg).unwrap()[6..]
    }
    fn parsed(jpeg: &[u8]) -> exif::Exif {
        exif::Reader::new()
            .read_from_container(&mut Cursor::new(jpeg))
            .unwrap()
    }

    pub(crate) fn synthetic_gps_jpeg() -> Vec<u8> {
        fixture(true, false, Some(64))
    }

    pub(crate) fn assert_synthetic_gps_metadata(jpeg: &[u8], width: u64, height: u64) {
        let metadata = parsed(jpeg);
        for (tag, expected) in [
            (exif::Tag::Orientation, 1),
            (exif::Tag::PixelXDimension, width as u32),
            (exif::Tag::PixelYDimension, height as u32),
        ] {
            assert_eq!(
                metadata
                    .get_field(tag, exif::In::PRIMARY)
                    .unwrap()
                    .value
                    .get_uint(0),
                Some(expected)
            );
        }
        assert!(metadata
            .get_field(exif::Tag::GPSLatitude, exif::In::PRIMARY)
            .is_some());
        assert!(metadata
            .get_field(exif::Tag::Software, exif::In::PRIMARY)
            .is_some());
        assert!(metadata
            .get_field(exif::Tag::MakerNote, exif::In::PRIMARY)
            .is_none());
    }

    #[test]
    fn preserves_both_byte_orders_and_dimension_types() {
        for little in [true, false] {
            for short in [true, false] {
                let source = fixture(little, short, Some(64));
                let developed = jpeg(&[
                    (0xe2, b"ICC_PROFILE\0synthetic"),
                    (0xe1, b"Exif\0\0old"),
                    (0xe1, b"http://ns.adobe.com/xap/1.0/\0xmp"),
                ]);
                let out = inherit_exif(&developed, &source, 80, 40, "GeoFrame RAW Assist").unwrap();
                let e = Endian(little);
                let b = tiff(&out);
                let original = tiff(&source);
                assert_eq!(&b[..2], &original[..2]);
                assert_eq!(&b[200..268], &original[200..268]); // GPS table including next-IFD
                assert_eq!(&b[360..408], &original[360..408]); // GPS rational bytes
                assert_eq!(&b[340..356], &original[340..356]); // orphan MakerNote + UserComment
                assert_eq!(&b[300..318], &original[300..318]); // Interop
                let d = ifd(b, e, 100).unwrap();
                assert_eq!(d.entries.len(), 4);
                assert!(tag(b, e, &d, 0x927c).unwrap().is_none());
                assert_eq!(e.u32(b, d.next).unwrap(), 0);
                assert_eq!(e.u32(b, 70).unwrap(), 0);
                let meta = parsed(&out);
                for (id, v) in [
                    (exif::Tag::Orientation, 1),
                    (exif::Tag::PixelXDimension, 80),
                    (exif::Tag::PixelYDimension, 40),
                ] {
                    assert_eq!(
                        meta.get_field(id, exif::In::PRIMARY)
                            .unwrap()
                            .value
                            .get_uint(0),
                        Some(v)
                    );
                }
                assert!(meta
                    .get_field(exif::Tag::MakerNote, exif::In::PRIMARY)
                    .is_none());
                assert!(meta
                    .get_field(exif::Tag::UserComment, exif::In::PRIMARY)
                    .is_some());
                assert_eq!(
                    segments(&out)
                        .unwrap()
                        .iter()
                        .filter(|s| s.marker == 0xe1 && out[s.payload.clone()].starts_with(EXIF))
                        .count(),
                    1
                );
                assert!(out.windows(21).any(|w| w == b"ICC_PROFILE\0synthetic"));
                // EXIF 以外の全セグメントはバイト単位で同一。
                let retained = |j: &[u8]| {
                    segments(j)
                        .unwrap()
                        .into_iter()
                        .filter(|s| !(s.marker == 0xe1 && j[s.payload.clone()].starts_with(EXIF)))
                        .flat_map(|s| j[s.range].to_vec())
                        .collect::<Vec<_>>()
                };
                assert_eq!(retained(&out), retained(&developed));
            }
        }
    }
    #[test]
    fn maker_removal_moves_nonzero_next_ifd_without_touching_other_entries() {
        let source = fixture(true, false, Some(64));
        let mut payload = exif_payload(&source).unwrap().to_vec();
        Endian(true).put32(&mut payload[6..], 162, 320);
        let out = inherit_exif(&jpeg(&[]), &jpeg(&[(0xe1, &payload)]), 80, 40, "x").unwrap();
        let b = tiff(&out);
        assert_eq!(Endian(true).u32(b, 150).unwrap(), 320);
        assert_eq!(&b[102..114], &payload[6 + 114..6 + 126]);
        assert_eq!(&b[138..150], &payload[6 + 150..6 + 162]);
    }

    #[test]
    fn software_inline_reuse_append_and_missing() {
        for little in [true, false] {
            for (capacity, text) in [
                (Some(64), "GeoFrame"),
                (Some(8), "GeoFrame RAW Assist (Core Image)"),
                (Some(4), "long software"),
                (Some(64), "x"),
                (Some(4), "x"),
                (None, "GeoFrame"),
                (None, "x"),
            ] {
                let source = fixture(little, false, capacity);
                let out = inherit_exif(&jpeg(&[]), &source, 80, 40, text).unwrap();
                let e = Endian(little);
                let b = tiff(&out);
                let d = ifd(b, e, e.u32(b, 4).unwrap() as usize).unwrap();
                let p = required(b, e, &d, 0x131).unwrap();
                assert_eq!(
                    &b[data_range(b, e, p).unwrap()],
                    format!("{text}\0").as_bytes()
                );
                if capacity.is_none() {
                    assert!(d.start >= 512);
                    assert_eq!(d.entries.len(), 6);
                } else {
                    assert_eq!(d.start, 8);
                }
                if capacity == Some(64) && text.len() > 4 {
                    assert_eq!(e.u32(b, p + 8).unwrap(), 432);
                }
                assert_eq!(e.u32(b, d.next).unwrap(), 0);
                assert_eq!(&b[200..268], &tiff(&source)[200..268]);
                assert!(parsed(&out)
                    .get_field(exif::Tag::Software, exif::In::PRIMARY)
                    .is_some());
            }
        }
    }
    #[test]
    fn invalid_inputs_and_limits() {
        let valid = fixture(true, true, Some(8));
        let empty = jpeg(&[]);
        assert_eq!(inherit_exif(&empty, &empty, 1, 1, "x"), Err(MissingExif));
        for bad in [
            vec![],
            vec![0xff, 0xd8],
            jpeg(&[(0xe1, b"Exif\0\0II")]),
            vec![0xff, 0xd8, 0xff, 0xe1, 0, 1, 0xff, 0xd9],
        ] {
            assert!(inherit_exif(&empty, &bad, 1, 1, "x").is_err());
        }
        assert_eq!(
            inherit_exif(&empty, &valid, 65536, 1, "x"),
            Err(DimensionOverflow)
        );
        assert_eq!(
            inherit_exif(&empty, &valid, 0, 1, "x"),
            Err(DimensionOverflow)
        );
        assert_eq!(
            inherit_exif(&empty, &valid, 1, 1, "a\0b"),
            Err(InvalidSoftware)
        );
        assert_eq!(
            inherit_exif(&empty, &valid, 1, 1, "日本語"),
            Err(InvalidSoftware)
        );
        assert_eq!(
            inherit_exif(&empty, &valid, 1, 1, &"x".repeat(65533)),
            Err(App1TooLarge)
        );
        let long = fixture(false, false, Some(8));
        assert!(inherit_exif(&empty, &long, u32::MAX, 1, "x").is_ok());
        for (pos, value) in [(4, 0xfffffff0), (50, 8), (234, 70000), (70, 8)] {
            let mut bad = tiff(&valid).to_vec();
            Endian(true).put32(&mut bad, pos, value);
            let mut payload = EXIF.to_vec();
            payload.extend_from_slice(&bad);
            assert!(inherit_exif(&empty, &jpeg(&[(0xe1, &payload)]), 1, 1, "x").is_err());
        }
        // APP1 のちょうど上限と、末尾追加による超過。
        let mut payload = exif_payload(&valid).unwrap().to_vec();
        payload.resize(MAX_APP1, 0);
        let large = jpeg(&[(0xe1, &payload)]);
        assert!(inherit_exif(&empty, &large, 1, 1, "x").is_ok());
        assert_eq!(
            inherit_exif(&empty, &large, 1, 1, "long software string"),
            Err(App1TooLarge)
        );
        // 全 prefix で panic せず拒否する。
        for n in 0..valid.len() {
            assert!(inherit_exif(&empty, &valid[..n], 1, 1, "x").is_err());
        }
        assert!(inherit_exif(b"bad", &valid, 1, 1, "x").is_err());
    }
    #[test]
    fn preserves_scan_bytes_and_removes_exif_between_scans() {
        let source = fixture(true, false, Some(8));
        let mut developed = jpeg(&[(0xda, b"scan header")]);
        developed.truncate(developed.len() - 2);
        developed.extend_from_slice(&[1, 0xff, 0, 2, 0xff, 0xd0, 3]);
        let extra = jpeg(&[(0xe1, b"Exif\0\0old"), (0xe2, b"icc")]);
        developed.extend_from_slice(&extra[2..]);
        let out = inherit_exif(&developed, &source, 1, 1, "x").unwrap();
        assert!(out.ends_with(&[
            1, 0xff, 0, 2, 0xff, 0xd0, 3, 0xff, 0xe2, 0, 5, b'i', b'c', b'c', 0xff, 0xd9
        ]));
    }

    /// 実機は読み取り専用。出力先も fixture にはせず work/ 内に限定する。
    /// python3 scripts/check_exif_inherit.py の前段として合成 JPEG に継承する。
    #[test]
    #[ignore = "manual: real Sony JPEGs; aggregate results only"]
    fn real_camera_app1() {
        use std::{fs, path::Path};
        let project = Path::new(env!("CARGO_MANIFEST_DIR")).parent().unwrap();
        let work = project.join("work/exif-108/rust");
        let developed = fs::read(work.join("synthetic.jpg")).unwrap();
        let source = project.join("work/raw");
        let mut count = 0;
        for path in fs::read_dir(source)
            .unwrap()
            .flatten()
            .map(|e| e.path())
            .filter(|p| {
                p.file_name()
                    .unwrap()
                    .to_string_lossy()
                    .starts_with("DSC074")
                    && p.extension().is_some_and(|x| x == "JPG")
            })
        {
            let original = fs::read(&path).unwrap();
            let out = inherit_exif(
                &developed,
                &original,
                80,
                40,
                "GeoFrame RAW Assist (Core Image)",
            )
            .unwrap();
            // 独立 reader でも読めることだけ検証。フィールド値はログに出さない。
            assert!(exif::Reader::new()
                .read_from_container(&mut Cursor::new(&out))
                .is_ok());
            fs::write(work.join(path.file_name().unwrap()), out).unwrap();
            count += 1;
        }
        assert_eq!(count, 11);
        println!("real-camera inheritance: {count} succeeded");
    }
}
