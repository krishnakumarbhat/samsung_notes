from __future__ import annotations

import io
import re
from collections import Counter
from pathlib import Path

from pypdf import PdfReader

from interview_coach.flow_01_models import ProjectSummary, ResumeProfile, RoleExperience

CANONICAL_KEYWORDS = {
    "rag": "RAG",
    "chromadb": "ChromaDB",
    "vllm": "vLLM",
    "llama.cpp": "llama.cpp",
    "onnx": "ONNX",
    "multimodal": "Multimodal Systems",
    "vlm": "VLM",
    "kv cache": "KV Cache",
    "llm": "LLM Systems",
    "prompt engineering": "Prompt Engineering",
    "slurm": "SLURM",
    "docker": "Docker",
    "singularity": "Singularity",
    "vectorization": "Vectorization",
    "parallelization": "Parallelization",
    "rl": "Reinforcement Learning",
    "ppo": "PPO",
    "sac": "SAC",
    "her": "HER",
    "behavior cloning": "Behavior Cloning",
    "llamaindex": "LlamaIndex",
    "langgraph": "LangGraph",
    "gemini": "Gemini",
    "pytest": "pytest",
    "sql": "SQL",
    "cuda": "CUDA",
    "pytorch": "PyTorch",
    "aws": "AWS",
    "hpc": "HPC",
}


class ResumeParser:
    def extract_profile(
        self, pdf_path: Path | None = None, manual_text: str | None = None
    ) -> ResumeProfile:
        raw_text = (manual_text or "").strip()
        if not raw_text and pdf_path and pdf_path.exists():
            raw_text = self.extract_text_from_pdf_path(pdf_path)
        return self._build_profile(raw_text)

    def extract_temporary_text(self, filename: str, content: bytes) -> str:
        if filename.lower().endswith(".pdf"):
            return self.extract_text_from_pdf_bytes(content)
        try:
            return content.decode("utf-8")
        except UnicodeDecodeError:
            return content.decode("latin-1", errors="ignore")

    def extract_text_from_pdf_path(self, pdf_path: Path) -> str:
        with pdf_path.open("rb") as handle:
            return self.extract_text_from_pdf_bytes(handle.read())

    def extract_text_from_pdf_bytes(self, content: bytes) -> str:
        reader = PdfReader(io.BytesIO(content))
        text = "\n".join(page.extract_text() or "" for page in reader.pages)
        return self._normalize_text(text)

    def _build_profile(self, raw_text: str) -> ResumeProfile:
        text = self._normalize_text(raw_text)
        if not text:
            return ResumeProfile()

        roles = self._extract_roles(text)
        projects = self._extract_projects(text)
        skills = self._extract_skills(text)
        focus_areas = self._extract_focus_areas(text, skills)
        return ResumeProfile(
            raw_text=text,
            roles=roles,
            projects=projects,
            skills=skills,
            focus_areas=focus_areas,
        )

    def _extract_roles(self, text: str) -> list[RoleExperience]:
        section = self._section_between(text, "Professional Experience", "Research & Projects")
        if not section:
            return []

        lines = [line.strip() for line in section.splitlines() if line.strip()]
        roles: list[RoleExperience] = []
        current_company = ""
        current_title = ""
        current_period = ""
        bullets: list[str] = []

        def flush_current() -> None:
            nonlocal current_company, current_title, current_period, bullets
            if current_company or current_title or bullets:
                roles.append(
                    RoleExperience(
                        company=current_company or "Unknown Company",
                        title=current_title or "Unknown Role",
                        period=current_period,
                        summary_bullets=bullets[:],
                        keywords=self._keywords_from_text(" ".join(bullets)),
                    )
                )
            current_company = ""
            current_title = ""
            current_period = ""
            bullets = []

        for line in lines:
            company_match = re.match(
                r"^(?P<company>[A-Za-z0-9 .&()/-]+?)\s+"
                "(?P<period>(?:[A-Za-z]{3}\\s+\\d{4}\\s+[\\u2013-]\\s+(?:Present|[A-Za-z]{3}\\s+\\d{4}))|"
                r"(?:Issued\s+[A-Za-z]{3}\s+\d{4})|(?:Remote))$",
                line,
            )
            title_match = re.match(
                r"^(?P<title>[A-Za-z0-9 ,&()/-]+?)\s+"
                r"(?P<location>(?:[A-Za-z .]+,\s*[A-Za-z .]+)|(?:Remote))$",
                line,
            )

            if line.startswith("•"):
                bullets.append(line.lstrip("• ").strip())
                continue
            if company_match and not any(token in line for token in ["[GitHub]", "[Link]"]):
                if current_company and bullets:
                    flush_current()
                current_company = company_match.group("company").strip()
                current_period = (company_match.group("period") or "").strip()
                continue
            if title_match and current_company and not current_title:
                current_title = title_match.group("title").strip()
                continue

        flush_current()
        return [role for role in roles if role.summary_bullets]

    def _extract_projects(self, text: str) -> list[ProjectSummary]:
        section = self._section_between(
            text, "Research & Projects", "Publications, Patents & Open Source"
        )
        if not section:
            return []

        lines = [line.strip() for line in section.splitlines() if line.strip()]
        projects: list[ProjectSummary] = []
        current_name = ""
        current_descriptor = ""
        current_bullets: list[str] = []

        def flush_current() -> None:
            nonlocal current_name, current_descriptor, current_bullets
            if current_name:
                projects.append(
                    ProjectSummary(
                        name=current_name,
                        descriptor=current_descriptor,
                        summary_bullets=current_bullets[:],
                        keywords=self._keywords_from_text(
                            " ".join([current_name, current_descriptor, *current_bullets])
                        ),
                    )
                )
            current_name = ""
            current_descriptor = ""
            current_bullets = []

        for line in lines:
            if line.startswith("•"):
                current_bullets.append(line.lstrip("• ").strip())
                continue
            if line.startswith("["):
                continue
            if current_name and current_bullets:
                flush_current()
            parts = [part.strip() for part in re.split(r"—|-", line, maxsplit=1)]
            current_name = parts[0]
            current_descriptor = parts[1] if len(parts) > 1 else ""

        flush_current()
        return [project for project in projects if project.summary_bullets]

    def _extract_skills(self, text: str) -> list[str]:
        section = (
            self._section_between(text, "Technical Skills", "")
            or text.split("Technical Skills")[-1]
        )
        skill_matches = re.findall(r"•\s+[^:]+:\s+(.+)", section)
        skills: list[str] = []
        for match in skill_matches:
            skills.extend(part.strip() for part in match.split(",") if part.strip())
        seen: set[str] = set()
        ordered: list[str] = []
        for skill in skills:
            normalized = skill.lower()
            if normalized in seen:
                continue
            seen.add(normalized)
            ordered.append(skill)
        return ordered

    def _extract_focus_areas(self, text: str, skills: list[str]) -> list[str]:
        lowered = text.lower()
        counter: Counter[str] = Counter()
        for needle, label in CANONICAL_KEYWORDS.items():
            count = lowered.count(needle)
            if count:
                counter[label] += count
        for skill in skills:
            compact = skill.strip()
            if compact:
                counter[compact] += 1
        return [item for item, _ in counter.most_common(12)]

    def _keywords_from_text(self, text: str) -> list[str]:
        lowered = text.lower()
        hits = [label for needle, label in CANONICAL_KEYWORDS.items() if needle in lowered]
        unique_hits: list[str] = []
        seen: set[str] = set()
        for hit in hits:
            key = hit.lower()
            if key in seen:
                continue
            seen.add(key)
            unique_hits.append(hit)
        return unique_hits[:8]

    def _normalize_text(self, text: str) -> str:
        text = text.replace("\r", "\n")
        text = re.sub(r"\n{3,}", "\n\n", text)
        text = re.sub(r"[ \t]+", " ", text)
        return text.strip()

    def _section_between(self, text: str, start: str, end: str) -> str:
        if start not in text:
            return ""
        section = text.split(start, maxsplit=1)[1]
        if end and end in section:
            section = section.split(end, maxsplit=1)[0]
        return section.strip()
