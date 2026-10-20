// GeoFrame RAW Assist — Core Image 後段カーネル（Issue #73、設計書 docs/design/coreimage-preview-adjustments.md §3.2）。
//
// rawpy 経路（local_raw/engine.py）の後段処理を厳密式で移植したもの:
//   geoframe_tone ... _apply_shadow_lift_array（lift）+ _apply_tone_array の brightness → contrast → saturation
//   geoframe_usm  ... _apply_tone_array の輝度限定アンシャープマスク（コアリング付き）と最終 [0,1] クリップ
//
// 入力 sample_t はすべて sRGB ガンマ空間の 0..1 の値（PostChain.swift が CILinearToSRGBToneCurve を掛けてから渡す）。
// このファイルは Sources/ の外に置く（SwiftPM が "unhandled file" で失敗するため）。
// ビルドは scripts/build-coreimage-kernels.sh（.metal → .air → .metallib → Generated/KernelLibrary.swift）。
// 変更したら必ずそのスクリプトで生成物を作り直し、生成物ごとコミットすること。

#include <CoreImage/CoreImage.h>

extern "C" {
namespace coreimage {

/// shadow_lift → [0,1] クリップ → brightness → contrast → saturation。
/// lift = shadow_lift / 100。2.0 = SHADOW_LIFT_GAIN / 255（engine.py）。
/// contrast 後はクリップしない（rawpy も最終段まで float のまま）。
float4 geoframe_tone(sample_t s, float lift, float brightness, float contrast, float saturation) {
    float3 x = clamp(s.rgb, 0.0f, 1.0f);
    float3 inv = 1.0f - x;
    x = clamp(x + lift * 2.0f * x * inv * inv, 0.0f, 1.0f);
    x = x * brightness;
    x = (x - 0.5f) * contrast + 0.5f;
    float y = dot(x, float3(0.2126f, 0.7152f, 0.0722f));
    x = y + (x - y) * saturation;
    return float4(x, s.a);
}

/// 輝度限定 USM。blurred.r に Rec.709 輝度 Y をぼかした値が入っている。
/// d = Y − Yblur をコアリングし、RGB に同量 d·gain を足して [0,1] にクリップする。
float4 geoframe_usm(sample_t s, sample_t blurred, float gain, float coring) {
    float y = dot(s.rgb, float3(0.2126f, 0.7152f, 0.0722f));
    float d = y - blurred.r;
    d = sign(d) * max(fabs(d) - coring, 0.0f);
    return float4(clamp(s.rgb + d * gain, 0.0f, 1.0f), s.a);
}

}
}
