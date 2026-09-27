"""Module: @role: HTMLドキュメントから不要タグを除去し、階層構造を保持したAI向け軽量Markdownテキストへ変換する。"""
import re
from html.parser import HTMLParser
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple, Union

from .token_counter import estimate_tokens

DISCARD_TAGS: Set[str] = {
    "script", "style", "noscript", "template", "svg", "iframe",
    "canvas", "nav", "footer", "head"
}

VOID_TAGS: Set[str] = {
    "area", "base", "br", "col", "embed", "hr", "img", "input",
    "link", "meta", "param", "source", "track", "wbr"
}

BLOCK_TAGS: Set[str] = {
    "address", "article", "aside", "blockquote", "dd", "div", "dl",
    "dt", "fieldset", "figcaption", "figure", "footer", "form",
    "h1", "h2", "h3", "h4", "h5", "h6", "header", "hr", "li",
    "main", "nav", "ol", "p", "pre", "section", "table", "tbody",
    "td", "tfoot", "th", "thead", "tr", "ul", "details", "summary"
}


class DocNode:
    def __init__(self, tag: str = "", attrs: Optional[Dict[str, str]] = None, parent: Optional['DocNode'] = None) -> None:
        self.tag: str = tag.lower()
        self.attrs: Dict[str, str] = attrs or {}
        self.parent: Optional['DocNode'] = parent
        self.children: List[Union['DocNode', str]] = []

    def add_child(self, child: Union['DocNode', str]) -> None:
        self.children.append(child)

    def text_content(self) -> str:
        parts: List[str] = []
        for child in self.children:
            if isinstance(child, str):
                parts.append(child)
            else:
                parts.append(child.text_content())
        return "".join(parts)

    def has_heading_child(self) -> bool:
        for child in self.children:
            if isinstance(child, DocNode):
                if child.tag in {"h1", "h2", "h3", "h4", "h5", "h6"}:
                    return True
                if child.tag in {"div", "header"} and child.has_heading_child():
                    return True
        return False


class _DocTreeParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root: DocNode = DocNode("root")
        self.current: DocNode = self.root
        self.discard_depth: int = 0

    def handle_starttag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]) -> None:
        tag_lower = tag.lower()
        attr_dict = {k.lower(): (v if v is not None else "") for k, v in attrs}

        # 広告やナビゲーション等の非コンテンツ要素を深さ管理で一括除外
        if tag_lower in DISCARD_TAGS:
            self.discard_depth += 1
            return
        if self.discard_depth > 0:
            return

        role = attr_dict.get("role", "").lower()
        if role in ("navigation", "banner", "contentinfo"):
            self.discard_depth += 1
            return

        node = DocNode(tag=tag_lower, attrs=attr_dict, parent=self.current)
        self.current.add_child(node)

        if tag_lower not in VOID_TAGS:
            self.current = node

    def handle_endtag(self, tag: str) -> None:
        tag_lower = tag.lower()
        if tag_lower in DISCARD_TAGS:
            if self.discard_depth > 0:
                self.discard_depth -= 1
            return
        if self.discard_depth > 0:
            return

        # 閉じタグ欠落や構文エラー時も親ノードへ安全に巻き戻し
        temp = self.current
        while temp and temp.tag != "root":
            if temp.tag == tag_lower:
                self.current = temp.parent if temp.parent else self.root
                break
            temp = temp.parent

    def handle_data(self, data: str) -> None:
        if self.discard_depth > 0 or not data:
            return
        self.current.add_child(data)


class DocRenderer:
    def render(self, root: DocNode) -> str:
        raw_md = self._render_node(root)
        cleaned = re.sub(r'\n{3,}', '\n\n', raw_md)
        cleaned_lines = [line.rstrip() for line in cleaned.splitlines()]
        return "\n".join(cleaned_lines).strip()

    def _render_node(self, node: DocNode) -> str:
        tag = node.tag

        if tag == "root":
            return self._render_children(node)

        if tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            level = int(tag[1])
            text = self._normalize_inline_text(node.text_content())
            return f"\n\n{'#' * level} {text}\n\n" if text else ""

        if tag in ("section", "article"):
            inner = self._render_children(node).strip()
            if not inner:
                return ""
            # 見出しタグを欠くセクションの識別子を疑似見出し化して階層を保護
            if not node.has_heading_child():
                sec_id = node.attrs.get("id") or node.attrs.get("aria-label") or node.attrs.get("title")
                if sec_id:
                    return f"\n\n### [Section: {sec_id}]\n\n{inner}\n\n"
            return f"\n\n{inner}\n\n"

        if tag == "p":
            text = self._render_children(node).strip()
            return f"\n\n{text}\n\n" if text else ""

        if tag == "pre":
            lang = self._detect_code_language(node)
            raw_text = node.text_content().replace("\r\n", "\n").strip("\n")
            return f"\n\n```{lang}\n{raw_text}\n```\n\n" if raw_text else ""

        if tag == "code":
            if node.parent and node.parent.tag == "pre":
                return node.text_content()
            code_text = node.text_content().strip()
            return f"`{code_text}`" if code_text else ""

        if tag in ("ul", "ol"):
            depth = self._get_list_depth(node)
            indent = "  " * max(0, depth - 1)
            is_ordered = (tag == "ol")
            lines: List[str] = []
            item_idx = 1
            for child in node.children:
                if isinstance(child, DocNode) and child.tag == "li":
                    prefix = f"{item_idx}. " if is_ordered else "* "
                    item_text = self._render_li(child)
                    if item_text:
                        lines.append(f"{indent}{prefix}{item_text}")
                        item_idx += 1
            return "\n" + "\n".join(lines) + "\n" if lines else ""

        if tag == "dl":
            inner = self._render_children(node).strip()
            return f"\n\n{inner}\n\n" if inner else ""

        if tag == "dt":
            text = self._normalize_inline_text(node.text_content())
            return f"\n\n**{text}**:\n" if text else ""

        if tag == "dd":
            text = self._normalize_inline_text(node.text_content())
            return f"  {text}\n" if text else ""

        if tag == "table":
            return self._render_table(node)

        if tag == "blockquote":
            inner = self._render_children(node).strip()
            if not inner:
                return ""
            quoted_lines = [f"> {line}" for line in inner.splitlines()]
            return "\n\n" + "\n".join(quoted_lines) + "\n\n"

        if tag == "details":
            summary_text = ""
            body_parts: List[str] = []
            for child in node.children:
                if isinstance(child, DocNode) and child.tag == "summary":
                    summary_text = self._normalize_inline_text(child.text_content())
                else:
                    body_parts.append(self._render_child(child))
            summary_title = summary_text or "Details"
            body_text = "".join(body_parts).strip()
            return f"\n\n#### ▼ [詳細: {summary_title}]\n\n{body_text}\n\n"

        if tag == "summary":
            return ""

        if tag == "a":
            href = node.attrs.get("href", "").strip()
            text = self._render_children(node).strip()
            if not text:
                return ""
            if not href or href.startswith("javascript:") or href.startswith("#"):
                return text
            if href == text:
                return f"<{href}>"
            return f"[{text}]({href})"

        if tag in ("strong", "b"):
            text = self._render_children(node).strip()
            return f"**{text}**" if text else ""

        if tag in ("em", "i"):
            text = self._render_children(node).strip()
            return f"*{text}*" if text else ""

        if tag == "br":
            return "\n"
        if tag == "hr":
            return "\n\n---\n\n"

        if tag == "img":
            alt = node.attrs.get("alt", "").strip()
            return f"![{alt}]" if alt else ""

        rendered = self._render_children(node)
        if tag in BLOCK_TAGS:
            return f"\n{rendered}\n"
        return rendered

    def _render_children(self, node: DocNode) -> str:
        parts: List[str] = []
        for child in node.children:
            parts.append(self._render_child(child))
        return "".join(parts)

    def _render_child(self, child: Union[DocNode, str]) -> str:
        if isinstance(child, str):
            return re.sub(r'[ \t\r\n]+', ' ', child)
        return self._render_node(child)

    def _render_li(self, li_node: DocNode) -> str:
        parts: List[str] = []
        for child in li_node.children:
            if isinstance(child, DocNode) and child.tag in ("ul", "ol"):
                parts.append("\n" + self._render_node(child).rstrip())
            else:
                parts.append(self._render_child(child))
        return "".join(parts).strip()

    def _get_list_depth(self, node: DocNode) -> int:
        depth = 0
        cur = node.parent
        while cur:
            if cur.tag in ("ul", "ol"):
                depth += 1
            cur = cur.parent
        return depth

    def _detect_code_language(self, pre_node: DocNode) -> str:
        classes = pre_node.attrs.get("class", "")
        for child in pre_node.children:
            if isinstance(child, DocNode) and child.tag == "code":
                classes += " " + child.attrs.get("class", "")
        match = re.search(r'(?:language-|lang-)([a-zA-Z0-9_+#-]+)', classes, re.IGNORECASE)
        return match.group(1).lower() if match else ""

    def _normalize_inline_text(self, text: str) -> str:
        return re.sub(r'\s+', ' ', text).strip()

    def _render_table(self, table_node: DocNode) -> str:
        rows: List[List[Tuple[bool, str]]] = []

        def collect_rows(curr: DocNode) -> None:
            for child in curr.children:
                if isinstance(child, DocNode):
                    if child.tag == "tr":
                        row_cells: List[Tuple[bool, str]] = []
                        for cell in child.children:
                            if isinstance(cell, DocNode) and cell.tag in ("th", "td"):
                                is_th = (cell.tag == "th")
                                cell_text = self._normalize_inline_text(self._render_children(cell)).replace("|", "\\|")
                                row_cells.append((is_th, cell_text))
                        if row_cells:
                            rows.append(row_cells)
                    elif child.tag in ("thead", "tbody", "tfoot"):
                        collect_rows(child)

        collect_rows(table_node)
        if not rows:
            return ""

        max_cols = max(len(r) for r in rows)
        if max_cols == 0:
            return ""

        for r in rows:
            while len(r) < max_cols:
                r.append((False, ""))

        has_th = any(cell[0] for cell in rows[0])
        lines: List[str] = []

        if has_th:
            header_cells = [cell[1] for cell in rows[0]]
            lines.append("| " + " | ".join(header_cells) + " |")
            lines.append("| " + " | ".join(["---"] * max_cols) + " |")
            data_rows = rows[1:]
        else:
            header_cells = [f"Col {i + 1}" for i in range(max_cols)]
            lines.append("| " + " | ".join(header_cells) + " |")
            lines.append("| " + " | ".join(["---"] * max_cols) + " |")
            data_rows = rows

        for r in data_rows:
            lines.append("| " + " | ".join([c[1] for c in r]) + " |")

        return "\n\n" + "\n".join(lines) + "\n\n"


def parse_html_to_markdown(html_text: str) -> str:
    """HTML文字列を解析し、不要要素を除去した階層Markdownテキストに変換する"""
    parser = _DocTreeParser()
    parser.feed(html_text)
    parser.close()
    return DocRenderer().render(parser.root)


def extract_html_docs(
    targets: List[Path],
    project_root: Optional[Path] = None,
) -> Tuple[str, int, int]:
    """対象パス群からHTMLドキュメントを探索・抽出し、軽量Markdownと統計量を返す"""
    root = (project_root or Path(".")).resolve()
    resolved_files: List[Path] = []

    for t in targets:
        path = (root / t).resolve() if not t.is_absolute() else t.resolve()
        if not path.exists():
            continue
        if path.is_file():
            resolved_files.append(path)
        elif path.is_dir():
            for ext in ("*.html", "*.htm", "*.xhtml"):
                resolved_files.extend(sorted(path.rglob(ext)))

    seen: Set[Path] = set()
    unique_files: List[Path] = []
    for f in resolved_files:
        if f not in seen:
            seen.add(f)
            unique_files.append(f)

    if not unique_files:
        raise FileNotFoundError(f"解析対象のHTMLファイルが見つかりません: {[str(t) for t in targets]}")

    docs_output: List[str] = []
    for f in unique_files:
        try:
            rel = f.relative_to(root).as_posix()
        except ValueError:
            rel = f.name
        content = f.read_text(encoding="utf-8", errors="replace")
        md = parse_html_to_markdown(content)
        docs_output.append(f"# Document: {rel}\n\n{md}")

    combined = "\n\n---\n\n".join(docs_output)
    est_tokens = estimate_tokens(combined)
    return combined, len(combined), est_tokens
