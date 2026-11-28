"""LLM-слой Fusion (ML_LLM_FUSION_SPEC.md §3, §5; skill `llm-output-validation`).

LLM только оценивает контекст (`LLMContext`) и новости (`LLMNewsScorer`): не создаёт сделку,
не разворачивает направление, не увеличивает экспозицию. Подмодули импортируются явно.
"""
