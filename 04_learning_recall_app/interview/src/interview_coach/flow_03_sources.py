from __future__ import annotations

from interview_coach.flow_01_models import InterviewSection, ResumeProfile, SourceTopic

SOURCE_CATALOG: list[SourceTopic] = [
    SourceTopic(
        name="LLM Interview Systems",
        url="https://github.com/llmgenai/LLMInterviewQuestions",
        categories=[
            "Prompting",
            "RAG",
            "Chunking",
            "Embeddings",
            "Vector Search",
            "Fine-Tuning",
            "Alignment",
            "Evaluation",
            "Hallucination Control",
            "Deployment",
            "Agents",
            "Prompt Security",
        ],
        interview_angles=[
            "trade-offs between RAG and fine-tuning",
            "retrieval failures and reranking",
            "KV-cache and inference optimization",
        ],
    ),
    SourceTopic(
        name="General LLM Concepts",
        url="https://github.com/Devinterview-io/llms-interview-questions",
        categories=[
            "Transformers",
            "Attention",
            "RoPE",
            "Grouped Query Attention",
            "Pretraining",
            "PEFT",
            "Long Context",
            "Applications",
        ],
        interview_angles=[
            "decoder-only architecture decisions",
            "memory and compute scaling",
            "attention variants in production",
        ],
    ),
    SourceTopic(
        name="Machine Learning Breadth",
        url="https://github.com/amitshekhariitbhu/machine-learning-interview-questions",
        categories=[
            "ML Fundamentals",
            "Optimization",
            "Model Evaluation",
            "System Design",
            "Behavioral",
            "Coding",
        ],
        interview_angles=[
            "production ML debugging",
            "metric selection under class imbalance",
            "MLOps and deployment trade-offs",
        ],
    ),
    SourceTopic(
        name="ML and CV Coding",
        url="https://github.com/andrewekhalel/MLQuestions",
        categories=[
            "Bias Variance",
            "Regularization",
            "Dimensionality Reduction",
            "CNN",
            "Computer Vision",
            "Coding Problems",
        ],
        interview_angles=[
            "algorithmic complexity under production constraints",
            "efficient image and matrix problems",
            "interview coding with ML flavor",
        ],
    ),
    SourceTopic(
        name="Reinforcement Learning",
        url="https://github.com/Devinterview-io/reinforcement-learning-interview-questions",
        categories=[
            "MDP",
            "Value Functions",
            "On-policy vs Off-policy",
            "Exploration vs Exploitation",
            "DQN",
            "Experience Replay",
        ],
        interview_angles=[
            "policy-value trade-offs",
            "sample efficiency",
            "stability in RL training",
        ],
    ),
    SourceTopic(
        name="Generative AI Breadth",
        url="https://github.com/aishwaryanr/awesome-generative-ai-guide",
        categories=[
            "RAG",
            "Fine-Tuning",
            "Evaluation",
            "Agents",
            "Research Trends",
            "MLOps",
        ],
        interview_angles=[
            "tooling choices for applied LLM systems",
            "evaluation discipline",
            "agent design and observability",
        ],
    ),
]


SECTION_PRIORITIES: dict[InterviewSection, list[str]] = {
    InterviewSection.RESUME: ["resume-driven systems", "production trade-offs"],
    InterviewSection.GITHUB: [
        "RAG",
        "Evaluation",
        "Transformers",
        "System Design",
        "Coding",
        "Agents",
    ],
    InterviewSection.DEEP_DIVE: [
        "RAG",
        "Transformers",
        "Deployment",
        "Evaluation",
        "MLOps",
    ],
    InterviewSection.CODING: ["Coding Problems", "Vector Search", "System Design"],
    InterviewSection.HR: ["Behavioral", "System Design", "Leadership"],
}


class SourceCatalogService:
    def topics_for_section(
        self,
        section: InterviewSection,
        resume_profile: ResumeProfile,
    ) -> list[str]:
        preferred = SECTION_PRIORITIES[section]
        collected: list[str] = []
        for source in SOURCE_CATALOG:
            for category in source.categories:
                if category in preferred or category in resume_profile.focus_areas:
                    collected.append(category)
        for focus_area in resume_profile.focus_areas:
            if focus_area not in collected:
                collected.append(focus_area)
        return collected[:12]

    def source_notes_for_section(
        self,
        section: InterviewSection,
        resume_profile: ResumeProfile,
    ) -> list[str]:
        relevant_topics = set(self.topics_for_section(section, resume_profile))
        notes: list[str] = []
        for source in SOURCE_CATALOG:
            overlap = [category for category in source.categories if category in relevant_topics]
            if not overlap and section == InterviewSection.GITHUB:
                overlap = source.categories[:3]
            if overlap:
                notes.append(f"{source.name}: {', '.join(overlap[:5])}. Source: {source.url}")
        return notes[:6]

    def coding_topics(self, resume_profile: ResumeProfile) -> list[str]:
        preferred_topics = [
            "RAG scheduling",
            "Top-k ranking",
            "Chunk merging",
            "KV-cache bookkeeping",
            "Streaming anomaly detection",
            "Graph search",
            "Interval scheduling",
        ]
        resume_enriched = [focus for focus in resume_profile.focus_areas if focus]
        return [*preferred_topics, *resume_enriched][:10]
