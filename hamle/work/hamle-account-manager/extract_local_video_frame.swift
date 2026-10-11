// Local, read-only video observation. No credentials, network, or provider writes.
import Foundation
import AVFoundation
import ImageIO
import UniformTypeIdentifiers

guard CommandLine.arguments.count == 4,
      let seconds = Double(CommandLine.arguments[2]), seconds >= 0, seconds <= 60 else {
    fatalError("usage: input.mp4 seconds output.png")
}
let source = URL(fileURLWithPath: CommandLine.arguments[1])
let output = URL(fileURLWithPath: CommandLine.arguments[3])
guard !FileManager.default.fileExists(atPath: output.path) else { fatalError("output already exists") }
let asset = AVURLAsset(url: source)
let generator = AVAssetImageGenerator(asset: asset)
generator.appliesPreferredTrackTransform = true
generator.requestedTimeToleranceBefore = .zero
generator.requestedTimeToleranceAfter = .zero
var actual = CMTime.zero
let frame = try generator.copyCGImage(at: CMTime(seconds: seconds, preferredTimescale: 25000), actualTime: &actual)
guard abs(actual.seconds - seconds) < 0.00001 else { fatalError("actual timestamp differs") }
guard let writer = CGImageDestinationCreateWithURL(output as CFURL, UTType.png.identifier as CFString, 1, nil) else { fatalError("png writer unavailable") }
CGImageDestinationAddImage(writer, frame, nil)
guard CGImageDestinationFinalize(writer) else { fatalError("png write failed") }
let result: [String: Any] = ["actual_seconds": actual.seconds, "requested_seconds": seconds,
    "width": frame.width, "height": frame.height, "output": output.path,
    "method": "Apple AVAssetImageGenerator exact time; local observation only"]
let data = try JSONSerialization.data(withJSONObject: result, options: [.sortedKeys])
print(String(data: data, encoding: .utf8)!)
