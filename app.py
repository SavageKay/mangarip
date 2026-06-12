"""
MangaRip — Flask Backend
  / (index)    : Download chapter via Selenium → zip (>10 files) or direct PDFs
  /compile     : Upload zip/mhtml → preview → compile PDF
  /convert     : Upload CBZ/CBR → convert to PDF  (up to 100 files)
  /renamer     : Batch-rename PDFs               (up to 100 files)
"""

import io
import os
import re
import unicodedata
import threading
import uuid
import zipfile
import tempfile
import shutil
from pathlib import Path
from flask import (
    Flask, render_template, request, jsonify,
    send_file, send_from_directory,
)
from PIL import Image

# ── App setup ────────────────────────────────────────────────────────────────
app = Flask(__name__)
app.config["DOWNLOAD_FOLDER"] = os.path.join(os.path.dirname(__file__), "downloads")
app.config["UPLOAD_FOLDER"]   = os.path.join(os.path.dirname(__file__), "uploads")
app.config["MAX_CONTENT_LENGTH"] = 1024 * 1024 * 1024  # 1 GB

BATCH_LIMIT     = 100
DIRECT_DL_LIMIT = 10          # ≤ this many files → serve individually, no zip
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".gif"}

jobs: dict = {}   # in-memory job store  {job_id: {status, progress, ...}}


# ── Utilities ────────────────────────────────────────────────────────────────

def get_session_dir(session_id: str) -> str:
    path = os.path.join(app.config["DOWNLOAD_FOLDER"], session_id)
    os.makedirs(path, exist_ok=True)
    return path


def sanitize_filename(name: str) -> str:
    """Strip characters illegal on Windows/Linux; safe fallback."""
    name = re.sub(r'[<>:"/\\|?*]', "", name)
    name = name.strip().strip(".")
    return name or "chapter"


def collect_images(root_dir: str) -> list[str]:
    """Recursively collect image paths sorted by filename."""
    results = []
    for dirpath, _, files in os.walk(root_dir):
        for f in files:
            if Path(f).suffix.lower() in IMAGE_EXTENSIONS:
                results.append(os.path.join(dirpath, f))
    return sorted(results, key=lambda p: Path(p).name)


def images_to_pdf(image_paths: list[str], pdf_path: str, progress_cb=None) -> int:
    """
    Convert a list of image paths to a single PDF using Pillow.
    Returns the number of pages written.
    """
    pages: list[Image.Image] = []
    total = len(image_paths)
    for i, p in enumerate(image_paths):
        if progress_cb:
            progress_cb(f"Loading image {i+1}/{total}…")
        try:
            img = Image.open(p).convert("RGB")
            # JPEG round-trip to avoid Pillow RGBA/P mode issues
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=92)
            buf.seek(0)
            pages.append(Image.open(buf))
        except Exception as e:
            print(f"  Skipping {p}: {e}")

    if not pages:
        raise ValueError("No valid images could be loaded.")

    if progress_cb:
        progress_cb("Compiling PDF…")

    pages[0].save(pdf_path, save_all=True, append_images=pages[1:])

    for pg in pages:
        try:
            pg.close()
        except Exception:
            pass

    return len(pages)


# ─────────────────────────────────────────────────────────────────────────────
#  DOWNLOAD PAGE  (/  and  /download)
# ─────────────────────────────────────────────────────────────────────────────

def _download_chapter_task(job_id: str, chapter_url: str, session_id: str):
    jobs[job_id]["status"]   = "downloading"
    jobs[job_id]["progress"] = "Starting browser…"

    chapter_name = sanitize_filename(chapter_url.rstrip("/").split("/")[-1])
    session_dir  = get_session_dir(session_id)
    output_dir   = os.path.join(session_dir, chapter_name)

    try:
        from selenium_webpage_downloader import download_with_selenium
        result = download_with_selenium(
            url=chapter_url,
            output_dir=output_dir,
            scroll_pause=1.5,
            max_scrolls=60,
            job_id=job_id,
            jobs=jobs,
        )
    except ImportError:
        jobs[job_id]["status"]   = "error"
        jobs[job_id]["progress"] = (
            "selenium_webpage_downloader not found. "
            "Run: pip install selenium webdriver-manager"
        )
        return
    except Exception as e:
        jobs[job_id]["status"]   = "error"
        jobs[job_id]["progress"] = str(e)
        return

    if not result:
        jobs[job_id]["status"]   = "error"
        jobs[job_id]["progress"] = "Download returned no result."
        return

    images_dir   = result.get("images_dir", os.path.join(output_dir, "images"))
    image_files  = result.get("image_files", [])

    if not image_files:
        # Collect whatever ended up in the images dir
        image_files = [
            Path(p).name for p in collect_images(images_dir)
        ]

    image_count = len(image_files)
    jobs[job_id]["progress"]     = f"Zipping {image_count} images…"
    jobs[job_id]["image_count"]  = image_count
    jobs[job_id]["chapter_name"] = chapter_name

    if image_count == 0:
        jobs[job_id]["status"]   = "error"
        jobs[job_id]["progress"] = "No chapter images found on that page."
        return

    # ── Build deliverable: zip (>DIRECT_DL_LIMIT) or individual files ────
    if image_count <= DIRECT_DL_LIMIT:
        # Store individual image paths for direct download
        file_list = []
        for fname in image_files:
            fpath = os.path.join(images_dir, fname)
            if os.path.exists(fpath):
                file_list.append({"name": fname, "path": fpath})

        jobs[job_id]["status"]     = "complete"
        jobs[job_id]["progress"]   = f"Done — {image_count} image(s) ready."
        jobs[job_id]["mode"]       = "direct"
        jobs[job_id]["files"]      = file_list
        jobs[job_id]["mhtml_path"] = result.get("mhtml_path")
    else:
        # Build a zip
        zip_path = os.path.join(session_dir, f"{chapter_name}.zip")
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for fname in image_files:
                fpath = os.path.join(images_dir, fname)
                if os.path.exists(fpath):
                    zf.write(fpath, fname)

        jobs[job_id]["status"]     = "complete"
        jobs[job_id]["progress"]   = f"Done — {image_count} images zipped."
        jobs[job_id]["mode"]       = "zip"
        jobs[job_id]["zip_path"]   = zip_path
        jobs[job_id]["zip_name"]   = f"{chapter_name}.zip"
        jobs[job_id]["mhtml_path"] = result.get("mhtml_path")


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/download", methods=["POST"])
def download():
    data       = request.json or {}
    url        = data.get("url", "").strip()
    session_id = data.get("session_id") or str(uuid.uuid4())

    if not url:
        return jsonify({"error": "URL is required"}), 400

    job_id = str(uuid.uuid4())
    jobs[job_id] = {
        "status":   "queued",
        "progress": "Queued…",
        "url":      url,
    }

    t = threading.Thread(
        target=_download_chapter_task,
        args=(job_id, url, session_id),
        daemon=True,
    )
    t.start()
    return jsonify({"job_id": job_id, "session_id": session_id})


@app.route("/job/<job_id>")
def job_status(job_id: str):
    job = jobs.get(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404
    return jsonify(job)


@app.route("/download_zip/<job_id>")
def download_zip(job_id: str):
    job = jobs.get(job_id)
    if not job or job.get("status") != "complete" or job.get("mode") != "zip":
        return "Zip not ready", 404
    zip_path = job.get("zip_path")
    if not zip_path or not os.path.exists(zip_path):
        return "File not found", 404
    return send_file(zip_path, as_attachment=True,
                     download_name=job.get("zip_name", "chapter.zip"))


@app.route("/download_image/<job_id>/<int:file_index>")
def download_image(job_id: str, file_index: int):
    """Serve a single image for direct-download mode (≤10 images)."""
    job = jobs.get(job_id)
    if not job or job.get("status") != "complete" or job.get("mode") != "direct":
        return "Not ready", 404
    files = job.get("files", [])
    if file_index >= len(files):
        return "Not found", 404
    entry = files[file_index]
    return send_file(entry["path"], as_attachment=True,
                     download_name=entry["name"])


# ─────────────────────────────────────────────────────────────────────────────
#  COMPILE PAGE  (/compile)
# ─────────────────────────────────────────────────────────────────────────────

@app.route("/compile")
def compile_page():
    return render_template("compile.html")


def _extract_mhtml_images(mhtml_path: str, extract_dir: str) -> list[dict]:
    """Use the downloader's MHTML extractor; fall back to email parser."""
    try:
        from selenium_webpage_downloader import extract_images_from_mhtml
        fnames = extract_images_from_mhtml(mhtml_path, extract_dir)
        return [{"filename": f, "rel_path": f, "url": f"/uploaded_image/_/{f}"}
                for f in fnames]
    except Exception:
        pass

    import email as _email
    IMAGE_TYPES = {"image/jpeg", "image/png", "image/webp", "image/gif"}
    ext_map = {
        "image/jpeg": ".jpg", "image/png": ".png",
        "image/webp": ".webp", "image/gif": ".gif",
    }
    saved = []
    idx = 0
    with open(mhtml_path, "rb") as f:
        msg = _email.message_from_bytes(f.read())
    for part in msg.walk():
        ct = part.get_content_type()
        if ct not in IMAGE_TYPES:
            continue
        data = part.get_payload(decode=True)
        if not data or len(data) < 1024:
            continue
        ext  = ext_map.get(ct, ".jpg")
        fname = f"{idx:04d}{ext}"
        out   = os.path.join(extract_dir, fname)
        with open(out, "wb") as f:
            f.write(data)
        saved.append({"filename": fname, "rel_path": fname})
        idx += 1
    return saved


@app.route("/upload_zip", methods=["POST"])
def upload_zip():
    file = (
        request.files.getlist("files")[0]
        if "files" in request.files
        else request.files.get("file")
    )
    if not file or file.filename == "":
        return jsonify({"error": "No file uploaded"}), 400

    fname = file.filename.lower()
    if not (fname.endswith(".zip") or fname.endswith(".mhtml")):
        return jsonify({"error": "Only .zip or .mhtml accepted"}), 400

    upload_id   = str(uuid.uuid4())
    extract_dir = os.path.join(app.config["UPLOAD_FOLDER"], upload_id)
    os.makedirs(extract_dir, exist_ok=True)

    if fname.endswith(".mhtml"):
        mhtml_path = os.path.join(extract_dir, "page.mhtml")
        file.save(mhtml_path)
        images = _extract_mhtml_images(mhtml_path, extract_dir)
        # Fix urls now that we have the upload_id
        for img in images:
            img["url"] = f"/uploaded_image/{upload_id}/{img['rel_path']}"
    else:
        zip_path = os.path.join(extract_dir, "upload.zip")
        file.save(zip_path)
        with zipfile.ZipFile(zip_path, "r") as zf:
            zf.extractall(extract_dir)

        all_files = []
        for root, _, files in os.walk(extract_dir):
            for f in files:
                if Path(f).suffix.lower() in IMAGE_EXTENSIONS:
                    full = os.path.join(root, f)
                    rel  = os.path.relpath(full, extract_dir).replace(os.sep, "/")
                    all_files.append((f, rel))

        images = [
            {
                "filename": f,
                "rel_path": rel,
                "url": f"/uploaded_image/{upload_id}/{rel}",
            }
            for f, rel in sorted(all_files, key=lambda x: x[0])
        ]

    return jsonify({"upload_id": upload_id, "images": images})


@app.route("/uploaded_image/<upload_id>/<path:rel_path>")
def serve_uploaded_image(upload_id: str, rel_path: str):
    # Sanitise to avoid path traversal
    safe_rel = re.sub(r"\.\.", "", rel_path)
    img_path = os.path.join(app.config["UPLOAD_FOLDER"], upload_id, safe_rel)
    if not os.path.exists(img_path):
        return "Not found", 404
    ext = Path(img_path).suffix.lower()
    mime_map = {
        ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
        ".png": "image/png",  ".webp": "image/webp",
        ".gif": "image/gif",
    }
    return send_file(img_path, mimetype=mime_map.get(ext, "image/jpeg"))


def _compile_pdf_task(
    job_id: str,
    upload_id: str,
    rel_paths: list[str],
    pdf_name: str,
):
    jobs[job_id]["status"] = "compiling"

    try:
        if not rel_paths:
            raise ValueError("No images selected.")

        safe_name  = sanitize_filename(pdf_name)
        upload_dir = os.path.join(app.config["UPLOAD_FOLDER"], upload_id)

        tmp = tempfile.NamedTemporaryFile(
            delete=False, suffix=".pdf",
            prefix=f"mangarip_{safe_name}_"
        )
        pdf_path = tmp.name
        tmp.close()

        full_paths = [os.path.join(upload_dir, rp) for rp in rel_paths]

        def cb(msg):
            jobs[job_id]["progress"] = msg

        pages = images_to_pdf(full_paths, pdf_path, progress_cb=cb)

        # Clean up upload dir now images are compiled
        shutil.rmtree(upload_dir, ignore_errors=True)

        jobs[job_id]["status"]   = "pdf_ready"
        jobs[job_id]["progress"] = f"PDF compiled — {pages} pages."
        jobs[job_id]["pdf_path"] = pdf_path
        jobs[job_id]["pdf_name"] = f"{safe_name}.pdf"

    except Exception as e:
        jobs[job_id]["status"]   = "error"
        jobs[job_id]["progress"] = str(e)


@app.route("/compile_pdf", methods=["POST"])
def compile_pdf():
    data      = request.json or {}
    upload_id = data.get("upload_id", "")
    rel_paths = data.get("rel_paths", [])
    pdf_name  = (data.get("pdf_name") or "chapter").strip()

    if not upload_id or not rel_paths:
        return jsonify({"error": "Missing upload_id or images"}), 400

    job_id = str(uuid.uuid4())
    jobs[job_id] = {"status": "queued", "progress": "Queued…"}

    t = threading.Thread(
        target=_compile_pdf_task,
        args=(job_id, upload_id, rel_paths, pdf_name),
        daemon=True,
    )
    t.start()
    return jsonify({"job_id": job_id})


@app.route("/download_pdf/<job_id>")
def download_pdf(job_id: str):
    job = jobs.get(job_id)
    if not job or job.get("status") != "pdf_ready":
        return "PDF not ready", 404
    pdf_path = job.get("pdf_path")
    if not pdf_path or not os.path.exists(pdf_path):
        return "File not found", 404

    with open(pdf_path, "rb") as f:
        data = f.read()

    try:
        os.remove(pdf_path)
    except Exception:
        pass
    jobs.pop(job_id, None)

    return send_file(
        io.BytesIO(data),
        as_attachment=True,
        download_name=job.get("pdf_name", "chapter.pdf"),
        mimetype="application/pdf",
    )


# ─────────────────────────────────────────────────────────────────────────────
#  CONVERT PAGE  (/convert)
# ─────────────────────────────────────────────────────────────────────────────

@app.route("/convert")
def convert_page():
    return render_template("convert.html")


def _convert_cbx_task(job_id: str, input_path: str, pdf_path: str, pdf_name: str):
    jobs[job_id]["status"] = "converting"
    temp_dir = None

    try:
        temp_dir = tempfile.mkdtemp()
        ext = Path(input_path).suffix.lower()

        if ext == ".cbz":
            jobs[job_id]["progress"] = "Extracting CBZ…"
            with zipfile.ZipFile(input_path, "r") as zf:
                zf.extractall(temp_dir)
        elif ext == ".cbr":
            jobs[job_id]["progress"] = "Extracting CBR…"
            try:
                import patoolib
                patoolib.extract_archive(input_path, outdir=temp_dir)
            except ImportError:
                raise RuntimeError("patool not installed. Run: pip install patool")
            except Exception as e:
                raise RuntimeError(f"CBR extraction failed: {e}")
        else:
            raise ValueError(f"Unsupported format: {ext}")

        all_images = collect_images(temp_dir)
        if not all_images:
            raise ValueError("No images found in the archive.")

        def cb(msg):
            jobs[job_id]["progress"] = msg

        pages = images_to_pdf(all_images, pdf_path, progress_cb=cb)

        jobs[job_id]["status"]   = "pdf_ready"
        jobs[job_id]["progress"] = f"Done — {pages} pages."
        jobs[job_id]["pdf_path"] = pdf_path
        jobs[job_id]["pdf_name"] = pdf_name

    except Exception as e:
        jobs[job_id]["status"]   = "error"
        jobs[job_id]["progress"] = str(e)
    finally:
        if temp_dir:
            shutil.rmtree(temp_dir, ignore_errors=True)


@app.route("/upload_cbx", methods=["POST"])
def upload_cbx():
    try:
        files = request.files.getlist("files")
        valid = [f for f in files if Path(f.filename).suffix.lower() in (".cbz", ".cbr")]

        if not valid:
            return jsonify({"error": "Only .cbz or .cbr files accepted"}), 400
        if len(valid) > BATCH_LIMIT:
            return jsonify({"error": f"Maximum {BATCH_LIMIT} files per batch."}), 400

        batch_id  = str(uuid.uuid4())
        batch_dir = os.path.join(app.config["UPLOAD_FOLDER"], batch_id)
        os.makedirs(batch_dir, exist_ok=True)

        job_ids = []
        for file in valid:
            ext      = Path(file.filename).suffix.lower()
            pdf_name = sanitize_filename(Path(file.filename).stem) + ".pdf"

            file_dir = os.path.join(batch_dir, str(uuid.uuid4()))
            os.makedirs(file_dir, exist_ok=True)

            input_path = os.path.join(file_dir, "input" + ext)
            file.save(input_path)
            pdf_path = os.path.join(file_dir, pdf_name)

            job_id = str(uuid.uuid4())
            jobs[job_id] = {
                "status":        "queued",
                "progress":      "Queued…",
                "pdf_name":      pdf_name,
                "original_name": file.filename,
            }

            t = threading.Thread(
                target=_convert_cbx_task,
                args=(job_id, input_path, pdf_path, pdf_name),
                daemon=True,
            )
            t.start()
            job_ids.append({
                "job_id":        job_id,
                "original_name": file.filename,
                "pdf_name":      pdf_name,
            })

        return jsonify({"batch_id": batch_id, "jobs": job_ids, "total": len(job_ids)})

    except Exception as e:
        return jsonify({"error": f"Server error: {str(e)}"}), 500


@app.route("/download_converted_zip", methods=["POST"])
def download_converted_zip():
    data     = request.get_json() or {}
    job_ids  = data.get("job_ids", [])
    zip_name = data.get("zip_name", "converted_pdfs.zip")

    if not job_ids:
        return jsonify({"error": "No job IDs provided"}), 400

    tmp   = tempfile.NamedTemporaryFile(delete=False, suffix=".zip")
    added = 0
    with zipfile.ZipFile(tmp.name, "w", zipfile.ZIP_DEFLATED) as zf:
        for jid in job_ids:
            job = jobs.get(jid)
            if job and job.get("status") == "pdf_ready":
                pp = job.get("pdf_path")
                if pp and os.path.exists(pp):
                    zf.write(pp, job.get("pdf_name", "chapter.pdf"))
                    added += 1

    if added == 0:
        os.unlink(tmp.name)
        return jsonify({"error": "No completed PDFs found"}), 400

    return send_file(
        tmp.name, as_attachment=True,
        download_name=zip_name, mimetype="application/zip",
    )


@app.route("/download_converted_single/<job_id>")
def download_converted_single(job_id: str):
    """Download a single converted PDF."""
    job = jobs.get(job_id)
    if not job or job.get("status") != "pdf_ready":
        return "Not ready", 404
    pp = job.get("pdf_path")
    if not pp or not os.path.exists(pp):
        return "Not found", 404

    with open(pp, "rb") as f:
        data = f.read()
    try:
        os.remove(pp)
    except Exception:
        pass

    folder = os.path.dirname(pp)
    shutil.rmtree(folder, ignore_errors=True)
    jobs.pop(job_id, None)

    return send_file(
        io.BytesIO(data),
        as_attachment=True,
        download_name=job.get("pdf_name", "chapter.pdf"),
        mimetype="application/pdf",
    )


@app.route("/cleanup_converted", methods=["POST"])
def cleanup_converted():
    data    = request.get_json() or {}
    job_ids = data.get("job_ids", [])
    deleted = 0
    for jid in job_ids:
        job = jobs.get(jid)
        if job:
            pp = job.get("pdf_path")
            if pp:
                folder = os.path.dirname(pp)
                if os.path.exists(folder):
                    shutil.rmtree(folder, ignore_errors=True)
                    deleted += 1
            jobs.pop(jid, None)
    return jsonify({"success": True, "deleted": deleted})


# ─────────────────────────────────────────────────────────────────────────────
#  RENAMER PAGE  (/renamer)
# ─────────────────────────────────────────────────────────────────────────────

RENAMER_FOLDER     = os.path.join(os.path.dirname(__file__), "renamed")
_ALLOWED_CHARS     = set("'\"()[]-_.,!& ")
_RENAMER_NUM_PAT   = re.compile(r"\|\|(\d+)\|\|")


def _sanitize_rename(name: str) -> str:
    name = unicodedata.normalize("NFKD", name)
    out  = "".join(
        ch if (ch.isalnum() or ch in _ALLOWED_CHARS) else "_"
        for ch in name
    )
    out  = re.sub(r"[\s_]+", " ", out).strip().lstrip(". ")
    return out or "unnamed_file"


def _generate_rename(base: str, index: int) -> str:
    stem, ext = os.path.splitext(base)
    m = _RENAMER_NUM_PAT.search(stem)
    if m:
        orig_num = m.group(1)
        new_num  = str(int(orig_num) + index).zfill(len(orig_num))
        new_stem = _RENAMER_NUM_PAT.sub(new_num, stem)
    else:
        new_stem = f"{stem} {index + 1}"
    return new_stem + ext


@app.route("/renamer")
def renamer_page():
    return render_template("renamer.html")


@app.route("/renamer/upload", methods=["POST"])
def renamer_upload():
    if "files" not in request.files:
        return jsonify({"error": "No files selected"}), 400

    files     = request.files.getlist("files")
    base_name = request.form.get("base_name", "").strip()

    if not base_name:
        return jsonify({"error": "Please provide a base name"}), 400
    if not files or all(f.filename == "" for f in files):
        return jsonify({"error": "No files selected"}), 400
    if len(files) > BATCH_LIMIT:
        return jsonify({"error": f"Maximum {BATCH_LIMIT} files per batch."}), 400

    if not base_name.endswith(".pdf"):
        base_name += ".pdf"

    os.makedirs(RENAMER_FOLDER, exist_ok=True)

    uploaded, errors = [], []
    for idx, file in enumerate(files):
        if not file or file.filename == "":
            continue
        if not file.filename.lower().endswith(".pdf"):
            errors.append(f"Skipped (not PDF): {file.filename}")
            continue
        try:
            raw_name   = _generate_rename(base_name, idx)
            safe_name  = _sanitize_rename(raw_name)

            # Handle duplicates
            counter = 1
            orig_safe = safe_name
            while os.path.exists(os.path.join(RENAMER_FOLDER, safe_name)):
                stem, ext  = os.path.splitext(orig_safe)
                safe_name  = f"{stem} ({counter}){ext}"
                counter   += 1

            fpath = os.path.join(RENAMER_FOLDER, safe_name)
            file.save(fpath)
            size_mb = os.path.getsize(fpath) / (1024 * 1024)
            uploaded.append({
                "original_name": file.filename,
                "new_name":      safe_name,
                "size":          f"{size_mb:.2f} MB",
            })
        except Exception as e:
            errors.append(f"Error renaming {file.filename}: {e}")

    resp = {
        "success":        True,
        "uploaded_files": uploaded,
        "total_uploaded": len(uploaded),
    }
    if errors:
        resp["errors"] = errors
    return jsonify(resp)


@app.route("/renamer/files")
def renamer_list_files():
    files = []
    if os.path.exists(RENAMER_FOLDER):
        for name in sorted(os.listdir(RENAMER_FOLDER)):
            if name.endswith(".pdf"):
                fp = os.path.join(RENAMER_FOLDER, name)
                files.append({
                    "name": name,
                    "size": f"{os.path.getsize(fp) / (1024 * 1024):.2f} MB",
                })
    return jsonify({"files": files})


@app.route("/renamer/download/<filename>")
def renamer_download(filename: str):
    return send_from_directory(RENAMER_FOLDER, filename, as_attachment=True)


@app.route("/renamer/download-multiple", methods=["POST"])
def renamer_download_multiple():
    data     = request.get_json() or {}
    selected = data.get("files", [])
    if not selected:
        return jsonify({"error": "No files selected"}), 400

    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".zip")
    with zipfile.ZipFile(tmp.name, "w", zipfile.ZIP_DEFLATED) as zf:
        for fn in selected:
            fp = os.path.join(RENAMER_FOLDER, fn)
            if os.path.exists(fp) and fn.endswith(".pdf"):
                zf.write(fp, fn)

    return send_from_directory(
        os.path.dirname(tmp.name),
        os.path.basename(tmp.name),
        as_attachment=True,
        download_name="renamed_pdfs.zip",
    )


@app.route("/renamer/clear", methods=["POST"])
def renamer_clear():
    deleted = 0
    if os.path.exists(RENAMER_FOLDER):
        for fn in os.listdir(RENAMER_FOLDER):
            if fn.endswith(".pdf"):
                try:
                    os.remove(os.path.join(RENAMER_FOLDER, fn))
                    deleted += 1
                except Exception:
                    pass
    return jsonify({"success": True, "message": f"Deleted {deleted} file(s)."})


# ─────────────────────────────────────────────────────────────────────────────
#  Entry point
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    os.makedirs(app.config["DOWNLOAD_FOLDER"], exist_ok=True)
    os.makedirs(app.config["UPLOAD_FOLDER"], exist_ok=True)
    os.makedirs(RENAMER_FOLDER, exist_ok=True)
    app.run(debug=True, port=5000)
