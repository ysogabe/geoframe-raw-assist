use super::metrics::Metrics;

pub fn guard_failure(style: &str, m: &Metrics, base: &Metrics) -> Option<String> {
    let high = m.highlight_clip - base.highlight_clip;
    if high >= 0.10 {
        return Some(format!(
            "白飛びが基準より{:.1}pt増えるため除外",
            high * 100.0
        ));
    }
    let shadow = m.shadow_clip - base.shadow_clip;
    if shadow >= 0.10 {
        return Some(format!(
            "黒つぶれが基準より{:.1}pt増えるため除外",
            shadow * 100.0
        ));
    }
    if style == "low_noise" && m.noise_flat > base.noise_flat {
        return Some("平坦部ノイズが基準より悪化するため除外".into());
    }
    if style == "foreground" && m.shadow_mean < base.shadow_mean + 0.005 {
        return Some("暗部が基準より明るくならないため除外".into());
    }
    None
}
pub fn score(m: &Metrics, style: &str, base: &Metrics) -> f64 {
    let mut penalties = m.highlight_clip * 42.0
        + m.shadow_clip * 32.0
        + m.noise_flat * if style == "low_noise" { 34.0 } else { 20.0 }
        + m.halo * 24.0
        + m.color_saturation_clip * 28.0
        + (0.62 - m.tonal_range).max(0.0) * 30.0
        + m.source_deviation * if style == "natural" { 26.0 } else { 10.0 };
    let mut quality = m.detail * 22.0;
    if style == "stars" {
        quality += (20.0 * m.star_count as f64 / base.star_count.max(1) as f64).min(20.0);
        penalties += m.star_halo * 24.0 + m.star_color_saturation * 10.0;
    }
    if style == "social" {
        quality += m.tonal_range * 8.0;
    }
    // Python round(value, 1) と同じく、元の二進浮動小数点数を十進へ丸める。
    // value * 10 を先に丸めると 28.349999999999998 が 28.4 になる。
    format!("{:.1}", (48.0 + quality - penalties).clamp(0.0, 100.0))
        .parse()
        .expect("formatted floating point score")
}
pub fn reasons(style: &str, m: &Metrics, base: &Metrics, star_scene: bool) -> Vec<String> {
    let mut r = Vec::new();
    if style != "natural" {
        if m.highlight_clip <= base.highlight_clip + 0.004 {
            r.push("明部の白飛びを基準より増やさない範囲で調整".into());
        }
        if m.shadow_clip < base.shadow_clip - 0.004 {
            r.push("暗部の黒つぶれを基準より抑えて階調を残す".into());
        }
    }
    match style {
        "natural" => r.push("プリセット値のままの基準候補（他候補はこの画像と比較）".into()),
        "stars" if star_scene => r.push(format!(
            "小さな高輝度点を優先（検出目安 {}点、基準 {}点）",
            m.star_count, base.star_count
        )),
        "foreground" => r.push("暗い前景を持ち上げ、ハイライト回復を併用".into()),
        "low_noise" => r.push("平坦な暗部のノイズを基準より抑える設定".into()),
        "social" => r.push("小さな表示でも見やすいコントラストと彩度".into()),
        _ => {}
    }
    if m.halo <= 0.12 {
        r.push("細部の強調によるハローを監視".into());
    }
    if r.is_empty() {
        r.push("代理画像の複数指標から選んだ非劣解".into());
    }
    r.truncate(4);
    r
}
