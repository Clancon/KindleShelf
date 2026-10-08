from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from urllib.parse import unquote, urlsplit

from bs4 import BeautifulSoup

from .articles import ArticleExtractor


class ConversionError(RuntimeError):
    pass


class CalibreConverter:
    """Small wrapper around Calibre's ebook-convert command."""

    def __init__(self, executable: str | Path | None = None) -> None:
        self.executable = self._find_executable(executable)

    @staticmethod
    def _find_executable(explicit: str | Path | None) -> Path | None:
        candidates: list[str | Path | None] = [
            explicit,
            os.environ.get("EBOOK_CONVERT"),
            shutil.which("ebook-convert"),
            Path(os.environ.get("ProgramFiles", "C:/Program Files"))
            / "Calibre2"
            / "ebook-convert.exe",
            Path(os.environ.get("ProgramFiles(x86)", "C:/Program Files (x86)"))
            / "Calibre2"
            / "ebook-convert.exe",
        ]
        for candidate in candidates:
            if candidate and Path(candidate).is_file():
                return Path(candidate).resolve()
        return None

    @property
    def available(self) -> bool:
        return self.executable is not None

    @staticmethod
    def _prepare_html(source: Path, work_dir: Path) -> Path:
        if source.stat().st_size > ArticleExtractor.MAX_PAGE_BYTES:
            raise ConversionError("HTML 文件过大，无法安全处理。")
        soup = BeautifulSoup(source.read_bytes(), "html.parser")
        resources: dict[int, tuple[str, bytes]] = {}
        source_root = source.parent.resolve()
        total = 0
        for image in soup.find_all("img")[: ArticleExtractor.MAX_IMAGES]:
            try:
                parsed = urlsplit(str(image.get("src") or ""))
                if parsed.scheme or parsed.netloc or not parsed.path:
                    continue
                resource = (source_root / unquote(parsed.path)).resolve()
                if (
                    not resource.is_relative_to(source_root)
                    or not resource.is_file()
                    or resource.stat().st_size > ArticleExtractor.MAX_IMAGE_BYTES
                ):
                    continue
                content = resource.read_bytes()
                total += len(content)
                if total > ArticleExtractor.MAX_TOTAL_IMAGE_BYTES:
                    break
                extension = ArticleExtractor.raster_extension(content)
                if extension:
                    resources[id(image)] = extension, content
            except (OSError, ValueError):
                continue
        ArticleExtractor._clean_content(soup, "")
        for number, image in enumerate(list(soup.find_all("img")), start=1):
            resource = resources.get(id(image))
            if resource is None:
                image.decompose()
                continue
            extension, content = resource
            filename = f"image-{number:02d}{extension}"
            (work_dir / filename).write_bytes(content)
            image["src"] = filename
        if not soup.get_text(" ", strip=True):
            raise ConversionError("HTML 文件没有可阅读的正文。")
        output = work_dir / "source.html"
        output.write_text(
            "<!doctype html><html><head><meta charset='utf-8'>"
            "<style>body{font-family:serif;line-height:1.65;margin:5%}"
            "img{max-width:100%;height:auto}pre{white-space:pre-wrap}</style>"
            f"</head><body>{soup}</body></html>",
            encoding="utf-8",
        )
        return output

    def convert(
        self,
        source: Path,
        destination: Path,
        *,
        title: str,
        author: str = "",
    ) -> None:
        if not self.executable:
            raise ConversionError(
                "没有找到 Calibre 的 ebook-convert。请安装 Calibre 后重新启动程序。"
            )

        try:
            source = source.resolve()
            destination = destination.resolve()
            if not source.is_file():
                raise ConversionError("找不到需要转换的文件。")
            destination.parent.mkdir(parents=True, exist_ok=True)
            # Write a complete result before replacing an existing destination.
            with tempfile.TemporaryDirectory(
                prefix=".conversion-", dir=destination.parent
            ) as temporary:
                work_dir = Path(temporary)
                html_input = source.suffix.lower() in {".html", ".htm", ".xhtml"}
                prepared = self._prepare_html(source, work_dir) if html_input else source
                output = work_dir / f"book{destination.suffix.lower()}"
                command = [
                    str(self.executable),
                    str(prepared),
                    str(output),
                    "--title",
                    title,
                    "--language",
                    "zh",
                    "--output-profile",
                    "kindle",
                ]
                if author:
                    command.extend(["--authors", author])
                if html_input:
                    command.extend(["--max-levels", "0"])
                result = subprocess.run(
                    command,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=300,
                    check=False,
                    creationflags=(
                        getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
                    ),
                )
                if result.returncode != 0 or not output.is_file() or output.stat().st_size == 0:
                    raise ConversionError(
                        "Calibre 转换失败，文件可能损坏或格式不受支持。请检查原文件后重试。"
                    )
                output.replace(destination)
        except subprocess.TimeoutExpired as exc:
            raise ConversionError("转换超过 5 分钟，已停止。") from exc
        except OSError as exc:
            raise ConversionError(
                "无法完成转换，请检查 Calibre 安装、文件权限和磁盘空间。"
            ) from exc
