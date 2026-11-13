from __future__ import annotations

from pathlib import Path

import streamlit as st

from interview_coach import InterviewService
from interview_coach.flow_01_models import InterviewSection, ResumeProfile

SECTIONS = [
    InterviewSection.RESUME,
    InterviewSection.GITHUB,
    InterviewSection.DEEP_DIVE,
    InterviewSection.CODING,
    InterviewSection.HR,
]


@st.cache_resource
def get_service() -> InterviewService:
    return InterviewService()


def key_for(prefix: str, section: InterviewSection) -> str:
    return f"{prefix}_{section.value}"


def ensure_resume_profile(service: InterviewService) -> ResumeProfile:
    manual_text = st.session_state.get("manual_resume_notes", "")
    default_path = service.settings.default_resume_path
    pdf_path = default_path if default_path.exists() else None
    profile = service.load_resume_profile(manual_text=manual_text, pdf_path=pdf_path)
    st.session_state["resume_profile"] = profile
    return profile


def current_jd_text(service: InterviewService) -> str | None:
    enabled = st.session_state.get("enable_jd", False)
    upload = st.session_state.get("jd_upload")
    if not enabled:
        st.session_state.pop("jd_text", None)
        return None
    if upload is not None:
        st.session_state["jd_text"] = service.extract_temporary_text(upload.name, upload.getvalue())
    return st.session_state.get("jd_text")


def render_section(
    service: InterviewService,
    section: InterviewSection,
    resume_profile: ResumeProfile,
    jd_text: str | None,
) -> None:
    current_question_key = key_for("current_question", section)
    answer_key = key_for("answer", section)
    last_record_key = key_for("last_record", section)
    due_reviews = service.due_count(section)

    top_left, top_right = st.columns([0.35, 0.65])
    with top_left:
        st.metric("Due Reviews", due_reviews)
    with top_right:
        st.caption(
            "Questions are generated from your resume, the source-topic catalog, "
            "past answers, and spaced-repetition history."
        )

    ask_key = key_for("ask_next", section)
    if st.button(f"Ask Next {section.label} Question", key=ask_key, use_container_width=True):
        st.session_state[current_question_key] = service.generate_question(
            section=section,
            resume_profile=resume_profile,
            jd_text=jd_text,
        )
        st.session_state[answer_key] = ""

    question = st.session_state.get(current_question_key)
    if question:
        st.subheader("Current Question")
        if question.active_recall_note:
            st.info(question.active_recall_note)
        st.write(question.question)
        with st.expander("Question Context"):
            st.write(question.rationale)
            if question.topics:
                st.write("Topics: " + ", ".join(question.topics))
            if question.source_inspiration:
                st.write("Source inspiration: " + " | ".join(question.source_inspiration))

        st.text_area(
            "Write your answer",
            key=answer_key,
            height=220,
            placeholder="Write the answer as if you are in a written interview round.",
        )
        submit_key = key_for("submit_answer", section)
        if st.button("Submit Answer", key=submit_key, type="primary", use_container_width=True):
            answer_text = st.session_state.get(answer_key, "").strip()
            if not answer_text:
                st.warning("Write an answer before submitting.")
            else:
                record = service.evaluate_answer(
                    candidate=question,
                    answer_text=answer_text,
                    resume_profile=resume_profile,
                    jd_text=jd_text,
                )
                st.session_state[last_record_key] = record
                st.session_state[current_question_key] = None
                st.session_state[answer_key] = ""
                st.rerun()

    last_record = st.session_state.get(last_record_key)
    if last_record:
        st.subheader("Latest Feedback")
        metric_left, metric_right = st.columns(2)
        with metric_left:
            st.metric("Score", f"{last_record.score}/5")
        with metric_right:
            st.metric("Next Review", last_record.due_at.strftime("%Y-%m-%d"))
        if last_record.strengths:
            st.write("Strengths")
            for strength in last_record.strengths:
                st.write(f"- {strength}")
        if last_record.gaps:
            st.write("Gaps")
            for gap in last_record.gaps:
                st.write(f"- {gap}")
        st.write("Coaching")
        st.write(last_record.coaching_note)
        if last_record.follow_up:
            st.write("Follow-up")
            st.write(last_record.follow_up)

    history = service.recent_history(section=section)
    if history:
        st.subheader("Recent History")
        rows = [
            {
                "Question": entry.question,
                "Score": entry.score,
                "Due": entry.due_at.strftime("%Y-%m-%d"),
                "Topics": ", ".join(entry.topics),
            }
            for entry in history
        ]
        st.dataframe(rows, use_container_width=True, hide_index=True)


def main() -> None:
    st.set_page_config(page_title="Interview Memory Coach", layout="wide")
    service = get_service()

    st.title("Interview Memory Coach")
    st.caption(
        "Local-first interview preparation with SQLite + ChromaDB memory, "
        "spaced repetition, and optional Gemini generation."
    )
    st.info(
        "Your answers stay local by default. JD uploads are temporary unless you "
        "explicitly enable external sync through environment variables."
    )

    with st.sidebar:
        st.header("Context")
        default_resume = service.settings.default_resume_path
        if Path(default_resume).exists():
            st.success(f"Default resume detected: {default_resume.name}")
        else:
            st.warning("No default resume PDF found. Use the manual notes field below.")

        st.text_area(
            "Manual resume notes override",
            key="manual_resume_notes",
            height=180,
            placeholder="Optional: add extra resume notes or corrections here.",
        )
        if st.button("Reload Resume Context", use_container_width=True):
            ensure_resume_profile(service)

        st.divider()
        st.toggle("Enable temporary JD targeting", key="enable_jd")
        st.file_uploader(
            "Upload JD (.txt, .md, .pdf)",
            type=["txt", "md", "pdf"],
            key="jd_upload",
            help=(
                "Stored only in session memory unless you explicitly enable external "
                "sync via env config."
            ),
        )

    resume_profile = ensure_resume_profile(service)
    jd_text = current_jd_text(service)

    summary_left, summary_right = st.columns(2)
    with summary_left:
        st.subheader("Resume Summary")
        st.write(
            resume_profile.compact_summary() or "Resume context will appear here after loading."
        )
    with summary_right:
        st.subheader("Focus Areas")
        if resume_profile.focus_areas:
            st.write(", ".join(resume_profile.focus_areas))
        else:
            st.write("No resume focus areas extracted yet.")

    tabs = st.tabs([section.label for section in SECTIONS])
    for tab, section in zip(tabs, SECTIONS, strict=False):
        with tab:
            render_section(service, section, resume_profile, jd_text)


if __name__ == "__main__":
    main()
