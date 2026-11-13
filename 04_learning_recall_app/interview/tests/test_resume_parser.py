from interview_coach.flow_02_resume import ResumeParser

SAMPLE_RESUME = """
Professional Experience
Aptiv                                                Jan 2024 - Present
AI Research Engineer (Systems)                       Bengaluru, India
• Architected Retrieval-Augmented Generation pipelines using ChromaDB.
• Optimized multimodal inference using vLLM, llama.cpp, and ONNX.

Research & Projects
Axiom — Automated Math Research Platform
• Built evaluation workflows with pytest and structured outputs.

Technical Skills
• Languages: Python, C++, CUDA
• ML, AI & Compilers: PyTorch, ONNX, ChromaDB, RAG
"""


def test_resume_parser_extracts_focus_areas() -> None:
    parser = ResumeParser()
    profile = parser.extract_profile(manual_text=SAMPLE_RESUME)

    assert "RAG" in profile.focus_areas
    assert "ChromaDB" in profile.focus_areas
    assert any(role.company == "Aptiv" for role in profile.roles)
    assert any(project.name.startswith("Axiom") for project in profile.projects)
