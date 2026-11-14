import Foundation
import Vision
import CoreImage
import CoreImage.CIFilterBuiltins
import AppKit
import PDFKit

struct CornerPoint: Codable {
    let x: Double
    let y: Double
}

struct DocumentCorners: Codable {
    let topLeft: CornerPoint
    let topRight: CornerPoint
    let bottomRight: CornerPoint
    let bottomLeft: CornerPoint
    let confidence: Float
}

struct OCRResult: Codable {
    let ok: Bool
    let pageCount: Int
    let text: String
    let corners: DocumentCorners?
    let outputPath: String?
    let sizeBytes: Int?
}

let ciContext = CIContext(options: [.useSoftwareRenderer: false])

func loadCGImage(from url: URL) -> CGImage? {
    guard let ciImage = CIImage(contentsOf: url, options: [.applyOrientationProperty: true]) else {
        if let nsImg = NSImage(contentsOf: url),
           let cg = nsImg.cgImage(forProposedRect: nil, context: nil, hints: nil) {
            return cg
        }
        return nil
    }
    return ciContext.createCGImage(ciImage, from: ciImage.extent)
}

func detectCorners(in cgImage: CGImage) -> DocumentCorners? {
    let req = VNDetectDocumentSegmentationRequest()
    let handler = VNImageRequestHandler(cgImage: cgImage, options: [:])
    do {
        try handler.perform([req])
        guard let obs = req.results?.first, obs.confidence > 0.6 else { return nil }
        return DocumentCorners(
            topLeft: CornerPoint(x: Double(obs.topLeft.x), y: Double(1.0 - obs.topLeft.y)),
            topRight: CornerPoint(x: Double(obs.topRight.x), y: Double(1.0 - obs.topRight.y)),
            bottomRight: CornerPoint(x: Double(obs.bottomRight.x), y: Double(1.0 - obs.bottomRight.y)),
            bottomLeft: CornerPoint(x: Double(obs.bottomLeft.x), y: Double(1.0 - obs.bottomLeft.y)),
            confidence: obs.confidence
        )
    } catch {
        return nil
    }
}

func perspectiveCorrectIfNeeded(_ ciImage: CIImage, cgImage: CGImage) -> CIImage {
    let req = VNDetectDocumentSegmentationRequest()
    let handler = VNImageRequestHandler(cgImage: cgImage, options: [:])
    do {
        try handler.perform([req])
        guard let obs = req.results?.first, obs.confidence > 0.78 else { return ciImage }
        let ext = ciImage.extent
        let w = ext.width
        let h = ext.height
        // Check that bounding box covers at least 25% of image so we don't crop to a tiny stamp
        let minX = min(obs.topLeft.x, obs.bottomLeft.x)
        let maxX = max(obs.topRight.x, obs.bottomRight.x)
        let minY = min(obs.bottomLeft.y, obs.bottomRight.y)
        let maxY = max(obs.topLeft.y, obs.topRight.y)
        if (maxX - minX) * (maxY - minY) < 0.25 {
            return ciImage
        }
        guard let filter = CIFilter(name: "CIPerspectiveCorrection") else { return ciImage }
        filter.setValue(ciImage, forKey: kCIInputImageKey)
        filter.setValue(CIVector(x: obs.topLeft.x * w, y: obs.topLeft.y * h), forKey: "inputTopLeft")
        filter.setValue(CIVector(x: obs.topRight.x * w, y: obs.topRight.y * h), forKey: "inputTopRight")
        filter.setValue(CIVector(x: obs.bottomRight.x * w, y: obs.bottomRight.y * h), forKey: "inputBottomRight")
        filter.setValue(CIVector(x: obs.bottomLeft.x * w, y: obs.bottomLeft.y * h), forKey: "inputBottomLeft")
        return filter.outputImage ?? ciImage
    } catch {
        return ciImage
    }
}

func applyDocumentFilter(_ ciImage: CIImage, mode: String) -> CIImage {
    var img = ciImage
    switch mode {
    case "magic":
        if let controls = CIFilter(name: "CIColorControls") {
            controls.setValue(img, forKey: kCIInputImageKey)
            controls.setValue(1.32, forKey: kCIInputContrastKey)
            controls.setValue(0.06, forKey: kCIInputBrightnessKey)
            controls.setValue(1.08, forKey: kCIInputSaturationKey)
            if let out = controls.outputImage { img = out }
        }
        if let sharpen = CIFilter(name: "CIUnsharpMask") {
            sharpen.setValue(img, forKey: kCIInputImageKey)
            sharpen.setValue(1.5, forKey: kCIInputRadiusKey)
            sharpen.setValue(0.65, forKey: kCIInputIntensityKey)
            if let out = sharpen.outputImage { img = out }
        }
    case "bw":
        if let noir = CIFilter(name: "CIPhotoEffectNoir") {
            noir.setValue(img, forKey: kCIInputImageKey)
            if let out = noir.outputImage { img = out }
        }
        if let controls = CIFilter(name: "CIColorControls") {
            controls.setValue(img, forKey: kCIInputImageKey)
            controls.setValue(1.65, forKey: kCIInputContrastKey)
            controls.setValue(0.10, forKey: kCIInputBrightnessKey)
            controls.setValue(0.0, forKey: kCIInputSaturationKey)
            if let out = controls.outputImage { img = out }
        }
    case "gray":
        if let controls = CIFilter(name: "CIColorControls") {
            controls.setValue(img, forKey: kCIInputImageKey)
            controls.setValue(1.22, forKey: kCIInputContrastKey)
            controls.setValue(0.03, forKey: kCIInputBrightnessKey)
            controls.setValue(0.0, forKey: kCIInputSaturationKey)
            if let out = controls.outputImage { img = out }
        }
    default:
        if let sharpen = CIFilter(name: "CIUnsharpMask") {
            sharpen.setValue(img, forKey: kCIInputImageKey)
            sharpen.setValue(1.2, forKey: kCIInputRadiusKey)
            sharpen.setValue(0.4, forKey: kCIInputIntensityKey)
            if let out = sharpen.outputImage { img = out }
        }
    }
    return img
}

func recognizeText(in cgImage: CGImage) -> String {
    let req = VNRecognizeTextRequest()
    req.recognitionLevel = .accurate
    req.usesLanguageCorrection = true
    req.recognitionLanguages = ["fr-FR", "en-US"]
    let handler = VNImageRequestHandler(cgImage: cgImage, options: [:])
    do {
        try handler.perform([req])
        guard let results = req.results else { return "" }
        var lines: [String] = []
        for obs in results {
            if let top = obs.topCandidates(1).first {
                lines.append(top.string)
            }
        }
        return lines.joined(separator: "\n")
    } catch {
        return ""
    }
}

func renderPDFPageToCGImage(_ page: PDFPage, dpi: CGFloat = 180.0) -> CGImage? {
    let bounds = page.bounds(for: .mediaBox)
    let scale = dpi / 72.0
    let width = max(Int(bounds.width * scale), 1)
    let height = max(Int(bounds.height * scale), 1)
    let colorSpace = CGColorSpaceCreateDeviceRGB()
    guard let ctx = CGContext(
        data: nil,
        width: width,
        height: height,
        bitsPerComponent: 8,
        bytesPerRow: 0,
        space: colorSpace,
        bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue
    ) else { return nil }
    ctx.setFillColor(CGColor(red: 1, green: 1, blue: 1, alpha: 1))
    ctx.fill(CGRect(x: 0, y: 0, width: width, height: height))
    ctx.saveGState()
    ctx.scaleBy(x: scale, y: scale)
    page.draw(with: .mediaBox, to: ctx)
    ctx.restoreGState()
    return ctx.makeImage()
}

func extractTextFromPDF(url: URL, maxPages: Int = 6) -> (Int, String) {
    guard let doc = PDFDocument(url: url) else { return (0, "") }
    let count = doc.pageCount
    var allTexts: [String] = []
    let limit = min(count, maxPages)
    for i in 0..<limit {
        guard let page = doc.page(at: i) else { continue }
        let embedded = (page.string ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
        if embedded.count > 40 {
            allTexts.append("--- Page \(i + 1) ---\n" + embedded)
        } else if let cg = renderPDFPageToCGImage(page) {
            let ocr = recognizeText(in: cg).trimmingCharacters(in: .whitespacesAndNewlines)
            if !ocr.isEmpty {
                allTexts.append("--- Page \(i + 1) ---\n" + ocr)
            }
        }
    }
    return (count, allTexts.joined(separator: "\n\n"))
}

let args = Array(CommandLine.arguments.dropFirst())
guard let mode = args.first else {
    fputs("Usage: vision_scanner <ocr|process> ...\n", stderr)
    exit(1)
}

if mode == "ocr" {
    guard args.count >= 2 else {
        fputs("Usage: vision_scanner ocr <file>\n", stderr)
        exit(1)
    }
    let fileURL = URL(fileURLWithPath: args[1])
    let ext = fileURL.pathExtension.lowercased()
    var pageCount = 1
    var fullText = ""
    var corners: DocumentCorners? = nil

    if ext == "pdf" {
        let (cnt, txt) = extractTextFromPDF(url: fileURL)
        pageCount = cnt
        fullText = txt
    } else if let cg = loadCGImage(from: fileURL) {
        corners = detectCorners(in: cg)
        fullText = recognizeText(in: cg)
    }

    let res = OCRResult(ok: true, pageCount: pageCount, text: fullText, corners: corners, outputPath: nil, sizeBytes: nil)
    let data = try! JSONEncoder().encode(res)
    FileHandle.standardOutput.write(data)
    exit(0)
} else if mode == "process" {
    var outputPath = ""
    var filterMode = "magic"
    var autocrop = false
    var inputFiles: [String] = []

    var i = 1
    while i < args.count {
        let a = args[i]
        if a == "--output" && i + 1 < args.count {
            outputPath = args[i + 1]
            i += 2
        } else if a == "--filter" && i + 1 < args.count {
            filterMode = args[i + 1]
            i += 2
        } else if a == "--autocrop" && i + 1 < args.count {
            autocrop = (args[i + 1] == "1" || args[i + 1] == "true")
            i += 2
        } else {
            inputFiles.append(a)
            i += 1
        }
    }

    guard !outputPath.isEmpty, !inputFiles.isEmpty else {
        fputs("Missing --output or input files\n", stderr)
        exit(1)
    }

    let pdfDoc = PDFDocument()
    var allTexts: [String] = []
    var totalPages = 0

    for inputPath in inputFiles {
        let url = URL(fileURLWithPath: inputPath)
        if url.pathExtension.lowercased() == "pdf" {
            if let existingPDF = PDFDocument(url: url) {
                for pIdx in 0..<existingPDF.pageCount {
                    if let p = existingPDF.page(at: pIdx) {
                        pdfDoc.insert(p, at: pdfDoc.pageCount)
                        totalPages += 1
                    }
                }
                let (_, txt) = extractTextFromPDF(url: url)
                if !txt.isEmpty { allTexts.append(txt) }
            }
            continue
        }

        guard var ciImage = CIImage(contentsOf: url, options: [.applyOrientationProperty: true]),
              let rawCG = ciContext.createCGImage(ciImage, from: ciImage.extent) else {
            continue
        }

        if autocrop {
            ciImage = perspectiveCorrectIfNeeded(ciImage, cgImage: rawCG)
        }
        ciImage = applyDocumentFilter(ciImage, mode: filterMode)

        // Downscale if huge (> 2400px on longest edge) to keep PDF crisp (~150-250 KB/page) and fast
        let ext = ciImage.extent
        let maxDim = max(ext.width, ext.height)
        if maxDim > 2200 {
            let scale = 2200.0 / maxDim
            ciImage = ciImage.transformed(by: CGAffineTransform(scaleX: scale, y: scale))
        }

        guard let finalCG = ciContext.createCGImage(ciImage, from: ciImage.extent) else { continue }
        let pageText = recognizeText(in: finalCG)
        if !pageText.isEmpty {
            allTexts.append("--- Page \(totalPages + 1) ---\n" + pageText)
        }

        let nsImg = NSImage(cgImage: finalCG, size: NSSize(width: finalCG.width, height: finalCG.height))
        if let pdfPage = PDFPage(image: nsImg) {
            pdfDoc.insert(pdfPage, at: pdfDoc.pageCount)
            totalPages += 1
        }
    }

    let outURL = URL(fileURLWithPath: outputPath)
    try? FileManager.default.createDirectory(at: outURL.deletingLastPathComponent(), withIntermediateDirectories: true)
    let ok = pdfDoc.write(to: outURL)
    let sizeBytes = (try? FileManager.default.attributesOfItem(atPath: outputPath)[.size] as? Int) ?? 0

    let res = OCRResult(
        ok: ok,
        pageCount: totalPages,
        text: allTexts.joined(separator: "\n\n"),
        corners: nil,
        outputPath: outputPath,
        sizeBytes: sizeBytes
    )
    let data = try! JSONEncoder().encode(res)
    FileHandle.standardOutput.write(data)
    exit(ok ? 0 : 1)
}
