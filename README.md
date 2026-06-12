# MangaRip — Manga Chapter Downloader & PDF Compiler

A Flask web app for downloading manga/manhwa chapters, compiling image archives into PDFs, converting CBR/CBZ comic files, and batch-renaming PDF chapters.

---

## Features

- **Download Page** (`/`) — Paste a chapter URL. Selenium renders the page in headless Chrome, scrolls to trigger lazy-loading, saves the full page as MHTML, then extracts and scores chapter images using URL patterns and dimension heuristics. Results are packaged as a ZIP (>10 images) or served as individual download links (≤10 images).
- **Compile PDF Page** (`/compile`) — Upload a `.zip` or `.mhtml` file, preview and select pages, reorder them, and compile to a named PDF.
- **CBR / CBZ Converter Page** (`/convert`) — Upload up to **100** `.cbz` or `.cbr` files at once. Each is individually converted to PDF. Download individually or grab all as a single ZIP.
- **PDF Renamer Page** (`/renamer`) — Batch-rename PDF chapters using a `||number||` template pattern that auto-increments across up to **100** files. Download individually or as a ZIP.

---

## Project Structure

```
MangaRip/
├── app.py                        ← Flask backend
├── selenium_webpage_downloader.py ← Selenium MHTML download + image extraction
├── templates/
│   ├── index.html                ← Download page
│   ├── compile.html              ← Compile PDF page
│   ├── convert.html              ← CBR/CBZ converter page
│   └── renamer.html              ← PDF renamer page
├── downloads/                    ← Auto-created; session chapter zips
├── uploads/                      ← Auto-created; temp extraction dirs
├── renamed/                      ← Auto-created; renamed PDFs
└── README.md
```

---

## Requirements

- Python 3.9+
- Google Chrome browser (for the Download page)

---

## Installation

### 1. Create and activate a virtual environment

**Windows:**
```bash
python -m venv .venv
.venv\Scripts\activate
```

**macOS / Linux:**
```bash
python3 -m venv .venv
source .venv/bin/activate
```

### 2. Install core dependencies

```bash
pip install flask pillow selenium requests beautifulsoup4 webdriver-manager
```

### 3. Install CBR support *(optional — only needed for `.cbr` files)*

`.cbz` files work out of the box (they are ZIP archives). For `.cbr` files you need `patool` and an unrar backend:

```bash
pip install patool
```

**Windows** — also install WinRAR or 7-Zip and make sure it's on your PATH.

**Ubuntu / Debian:**
```bash
sudo apt-get install unrar
```

**macOS:**
```bash
brew install unar
```

### 4. All-in-one install

```bash
pip install flask pillow selenium requests beautifulsoup4 webdriver-manager patool
```

---

## Running the App

```bash
python app.py
```

Open your browser at:
```
http://127.0.0.1:5000
```

---

## Usage

### Download a Chapter (`/`)
1. Paste a chapter URL (Asura Scans or similar manga reader sites).
2. Click **Download** — Selenium opens Chrome headlessly, scrolls the full page to trigger lazy-loading, saves it as MHTML, then extracts chapter images using scoring heuristics (URL patterns, image dimensions, parent container context).
3. **≤ 10 images** — individual download links appear per image.  
   **> 10 images** — a single ZIP download button appears.

### Compile a PDF (`/compile`)
1. Upload a `.zip` (from the Download page) or `.mhtml` (browser-saved full page).
2. Pages appear in the preview grid — all selected by default.
3. Toggle individual pages, reorder via sort controls, enter a PDF filename, and click **Compile to PDF**.
4. Download the PDF directly from the sidebar once ready.

### Convert CBR / CBZ (`/convert`)
1. Upload up to **100** `.cbz` or `.cbr` files.
2. Each file is queued and converted independently with a live progress indicator.
3. Download each PDF individually as it completes, or use **Download All** to grab a ZIP of all completed PDFs at once.

### Rename PDFs (`/renamer`)
1. Enter a base filename pattern using `||number||` to mark the incrementing position.  
   Example: `"My Manga ||1||"` → `My Manga 1.pdf`, `My Manga 2.pdf`, …
2. Upload up to **100** PDF files.
3. Files are renamed server-side and appear in the list. Download individually or select multiple and download as a ZIP.
4. Use **Clear All** to purge server-side renamed files when done.

---

## Notes

- **Smart image filtering** — The downloader scores each image by URL keywords (rejecting avatars, icons, banners) and dimensions (accepting portrait images in typical manga width ranges). Parent container HTML classes provide an additional signal.
- **MHTML fallback** — If the MHTML extraction yields fewer than 3 images, the downloader falls back to downloading the scored image URLs directly via `requests`.
- **PDF naming** — Characters like `?`, `:`, `/`, `\` are stripped automatically.
- **Batch limits** — Convert and Renamer pages accept up to **100 files** per batch.
- **Download mode** — Chapters with **≤ 10 images** are served as individual files; larger chapters are packaged into a ZIP.
- **Memory** — All images are loaded into RAM during PDF compilation. 200 pages at ~100 MB compiles cleanly on any modern machine.
- **Upload limit** — Files up to 1 GB are accepted.
- **CBR vs CBZ** — CBZ is a ZIP and needs no extra tools. CBR is a RAR archive and requires `patool` + an unrar utility installed on the system.
- **Chrome requirement** — The Download page needs Google Chrome installed. `webdriver-manager` downloads the matching ChromeDriver automatically.
