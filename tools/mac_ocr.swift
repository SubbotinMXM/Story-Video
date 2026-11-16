import Foundation
import Vision
import AppKit
// mac_ocr <картинка> <языки через запятую> → по строке на надпись: «текст<TAB>x,y,w,h» (рамка в долях картинки, от низа слева; без автоисправления)
let args = CommandLine.arguments
guard args.count >= 2, let img = NSImage(contentsOfFile: args[1]),
      let cg = img.cgImage(forProposedRect: nil, context: nil, hints: nil) else { print("ERR"); exit(1) }
let langs = args.count >= 3 ? args[2].split(separator: ",").map(String.init) : ["ru-RU", "en-US"]
let req = VNRecognizeTextRequest()
req.recognitionLevel = .accurate
req.usesLanguageCorrection = false
req.recognitionLanguages = langs
try VNImageRequestHandler(cgImage: cg, options: [:]).perform([req])
for obs in (req.results ?? []) {
    guard let c = obs.topCandidates(1).first else { continue }
    let b = obs.boundingBox  // доли картинки, начало координат — снизу слева
    print("\(c.string)\t\(b.minX),\(b.minY),\(b.width),\(b.height)")
}
