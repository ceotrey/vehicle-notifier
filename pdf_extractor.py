import io
import logging

logger = logging.getLogger(__name__)


def extract_text_from_pdf(pdf_bytes: bytes) -> str:
    """Extract text from PDF bytes. Falls back to OCR if no text layer found."""
    text = _extract_with_pdfplumber(pdf_bytes)
    if text.strip():
        return text
    logger.info("pdfplumber returned no text — trying OCR fallback")
    return _extract_with_ocr(pdf_bytes)


def _extract_with_pdfplumber(pdf_bytes: bytes) -> str:
    try:
        import pdfplumber
        pages_text = []
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            for page in pdf.pages:
                page_text = page.extract_text()
                if page_text:
                    pages_text.append(page_text)
        return "\n".join(pages_text)
    except Exception as e:
        logger.error(f"pdfplumber extraction error: {e}")
        return ""


def _extract_with_ocr(pdf_bytes: bytes) -> str:
    try:
        import fitz  # PyMuPDF
        import pytesseract
        from PIL import Image

        pages_text = []
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
        for page in doc:
            pix = page.get_pixmap(dpi=300)
            img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
            text = pytesseract.image_to_string(img)
            if text:
                pages_text.append(text)
        doc.close()
        return "\n".join(pages_text)
    except ImportError:
        pass
    except Exception as e:
        logger.error(f"OCR extraction error: {e}")

    # Fallback: convert PDF pages to images via pdf2image if PyMuPDF unavailable
    try:
        from pdf2image import convert_from_bytes
        import pytesseract

        images = convert_from_bytes(pdf_bytes, dpi=300)
        return "\n".join(pytesseract.image_to_string(img) for img in images)
    except Exception as e:
        logger.error(f"pdf2image OCR fallback error: {e}")
        return ""
