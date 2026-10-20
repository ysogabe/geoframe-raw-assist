// swift-tools-version:5.9
// GeoFrame RAW Assist — Core Image RAW 現像 PoC CLI（Issue #65）。
// 言語モードは Swift 5（CLI は同期処理のみで strict concurrency の恩恵が無いため）。
import PackageDescription

let package = Package(
    name: "geoframe-ci-develop",
    platforms: [.macOS(.v13)],
    products: [
        .executable(name: "geoframe-ci-develop", targets: ["geoframe-ci-develop"]),
    ],
    targets: [
        // リクエスト検証・現像・応答 JSON をまとめたライブラリ（テストから @testable import する）。
        .target(name: "CoreImageDevelop"),
        // stdin/stdout の入出力だけを担う薄い実行ターゲット。
        .executableTarget(name: "geoframe-ci-develop", dependencies: ["CoreImageDevelop"]),
        .testTarget(name: "CoreImageDevelopTests", dependencies: ["CoreImageDevelop"]),
    ]
)
