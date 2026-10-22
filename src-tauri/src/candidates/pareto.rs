/// 最小化の非劣解の元インデックス。重複点は残し、Python と同じ 1e-12 の許容幅を使う。
pub fn pareto_front(points: &[Vec<f64>]) -> Vec<usize> {
    (0..points.len())
        .filter(|&i| {
            !points.iter().enumerate().any(|(j, left)| {
                j != i
                    && left.len() == points[i].len()
                    && left.iter().zip(&points[i]).all(|(a, b)| *a <= *b + 1e-12)
                    && left.iter().zip(&points[i]).any(|(a, b)| *a < *b - 1e-12)
            })
        })
        .collect()
}
