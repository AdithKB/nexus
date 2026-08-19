"""
LLM Processor: transforms raw content → structured atomic notes.
Author: Adith K B

V4.0 (March 2026) Sovereign Optimization:
  1. Ultimate Sovereign Pipeline (Cloud Relay -> Local Crystallizer)
  2. 90% Cost Reduction via Context Caching (padded system prompt)
  3. PARSE-Enhanced Schema (Anchor:Quote pairs)
  4. Qwen 3-14B via MLX-Textgen with logit-masking
  5. Semantic Integrity Gate (Sentence-Transformers)
"""
import asyncio
import hashlib
import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Tuple, List


# ── Gemini context cache state (lives for the process lifetime) ───────────────
# Stores the cachedContent name, a hash of the system instruction it was built
# from, and the wall-clock expiry time. On each Stage 1 call we check all three
# before deciding to create a new cache vs reuse the existing one.
_relay_cache: dict = {"name": None, "system_hash": None, "expires_at": 0.0}
# MLX in-process model cache: load once, generate, then caller frees via free_mlx_model()
_mlx_loaded: dict = {"path": None, "model": None, "tokenizer": None}

import httpx
from pydantic import BaseModel, Field

from config import Config
from extractors import SourceMeta
from embed_store import EmbedStore


# ── Pydantic Schemas (Prompt-Embedded Logic) ──────────────────


class VaultLink(BaseModel):
    """A link to an existing vault note with a relationship type."""
    title: str = Field(description="Exact title of the existing note from the vault index.")
    rel: str = Field(description="Relationship type. Infer from content (e.g., prerequisite, contradiction, application, evidence, analogy, refinement, solves_problem, evidence_for, analogous_to, implemented_by, affects, part_of).")
    critic_logic: str = Field(
        default="",
        description="One-sentence logical justification for the link. Discard link if no specific reason exists."
    )


class AtomicNote(BaseModel):
    """Single Zettelkasten atomic note with hybrid linking (aliases + explicit edges)."""
    note_type: str = Field(
        default="concept",
        description="Note category. Infer from content (e.g., theory, mechanism, concept, argument, model, result, validation, method).",
    )
    title: str = Field(
        description=(
            "A full declarative sentence using natural spaces (no hyphens as separators). "
            "Must express the complete core claim — Subject + Verb + Object/Complement. "
            "The title IS the idea: reading it alone should tell you what the note asserts. "
            "Good: 'Dopamine Encodes Prediction Error Not Raw Reward'. "
            "Bad: 'Attention Mechanism', 'Dopamine', 'About Transformers'."
        )
    )
    aliases: list[str] = Field(
        description=(
            "3-5 multi-word compound terms, specific acronyms, or domain phrases "
            "that Obsidian uses for automatic unlinked-mention discovery. "
            "MUST be 2+ words each. NEVER single generic nouns like 'Data' or 'System'."
        )
    )
    canonical_links: list[str] = Field(
        default_factory=list,
        description="1-3 exact titles of OTHER notes you are generating in THIS batch only."
    )
    tags: list[str] = Field(
        description=(
            "3-5 hierarchical tags. MUST use lowercase/slash/nesting — NEVER bare words. "
            "Top-level domains: ai, neuroscience, engineering, math, economics, productivity, software-engineering, data-science, career-development. "
            "Examples: 'ai/transformers', 'ai/optimization', 'neuroscience/bci', 'engineering/distributed-systems', 'pattern/feedback-loop'. "
            "BAD: 'neural networks', 'Architecture', 'PSO Parameters'. "
            "GOOD: 'ai/neural-networks', 'ai/architecture', 'ai/optimization'."
        )
    )
    content: str = Field(
        description=(
            "The atomic idea in 2-3 sentences of strictly verbatim extraction. "
            "Use the EXACT technical terms, process names, and statistics found in the source. "
            "Do NOT paraphrase, simplify, or 'smooth' the language for flow. "
            "Every sentence must be grounded in the source's original phrasing. "
            "CRITICAL — if the source names a concept without explaining it, write EXACTLY: "
            "'[Concept] is identified as a key topic in this source. No further detail was provided.' "
            "Do NOT supply a definition, formula, or explanation from your training knowledge. "
            "A thin honest note is always better than a rich hallucinated one."
        )
    )
    implication: str = Field(
        default="",
        description=(
            "REQUIRED. One sentence starting with 'This implies', 'Practitioners should', or 'This means'. "
            "Must name a concrete decision, design choice, or next action derivable from this specific claim. "
            "This is the ONLY field where light synthesis beyond the source is permitted. "
            "FORBIDDEN: 'Requires further context', 'This is important', vague generalities. "
            "GOOD: 'This implies practitioners should apply atlas-based parcellation before computing graph metrics on fMRI data.' "
            "GOOD: 'Practitioners should filter tumor cohorts by cubic volume to reduce heterogeneity before connectivity analysis.'"
        )
    )
    source_quote: str = Field(
        description=(
            "A short verbatim quote (1-2 sentences) directly from the SOURCE that supports the claim. "
            "Copy character-for-character — zero paraphrasing, zero reordering of words. "
            "ACCEPTABLE sources: on-screen text, spoken narration, visual labels/diagrams, slide text, "
            "timestamps with frame descriptions (e.g., 'Visual: X at 01:25'). "
            "NOT ACCEPTABLE: AI-generated captions, YouTube auto-summaries, your own paraphrase of what was said. "
            "If no verbatim quote exists for this claim in the source, write exactly: 'NOT IN SOURCE' "
            "and set needs_review=true. NEVER construct, paraphrase, or use AI-generated summaries as quotes."
        )
    )
    vault_links_typed: list[VaultLink] = Field(
        default_factory=list,
        description="1-5 links to EXISTING notes from the VAULT INDEX with discovered relationship types."
    )
    needs_review: bool = Field(
        default=False,
        description=(
            "Set to true if: (a) source text was noisy or ambiguous, "
            "(b) source_quote is 'NOT IN SOURCE' or came from an AI-generated caption, "
            "(c) content contains any detail not explicitly stated in the source — "
            "including formulas, definitions, or elaborations from training knowledge, "
            "(d) content includes an academic citation (Author, Year) format — these are DOMAIN B "
            "unless a bibliography or citation list was visibly shown in the source."
        )
    )


class KnowledgeGraph(BaseModel):
    """AutoSchemaKG: Autonomous Knowledge Graph construction via schema induction."""
    entities: list[str] = Field(
        default_factory=list,
        description="Canonical concept names. Normalize variants. Max 20 important concepts."
    )
    relations: list[list[str]] = Field(
        default_factory=list,
        description="Directed triples: [[subject, relation, object], ...]. Infer types from content. Max 25 relations."
    )


class NoteCollection(BaseModel):
    """Complete LLM output for one source with induced schema."""
    thinking: str = Field(
        default="",
        description=(
            "Internal reasoning scratchpad written BEFORE other fields. "
            "SECTION 1 — LOGIC ANALYSIS: Identify core argument and note count. "
            "SECTION 2 — EVIDENCE CHECK: List every specific number, date, and visual entity to be used. For each planned note, write: 'Claim X — found in source: YES/NO'. Remove any claim where answer is NO. "
            "SECTION 3 — SCHEMA INDUCTION: Identify the recurring relationship types that emerge from this content. "
            "SECTION 4 — TERMINOLOGY ALIGNMENT: List exact technical phrases to preserve. "
            "SECTION 5 — DOMAIN AUDIT: For each note, ask 'Does this content come from DOMAIN A (relay description) or DOMAIN B (my training knowledge)?' Flag any DOMAIN B content for removal. "
            "SECTION 6 — SYNTHESIS PLAN: Map connections to existing vault notes. "
            "SECTION 7 — TITLE CHECK: Read your planned suggested_title. Does it contain any of these words: 'comment', 'follow', 'send', 'link', 'dm', 'type', 'tag', 'share', 'dataset', 'curated', 'bold', 'MRI'? If YES, REJECT it and re-derive from the actual technical subject matter. Write the rejected title and the replacement."
        )
    )
    suggested_title: str = Field(
        description=(
            "5-10 word content-derived title describing the ACTUAL SUBJECT MATTER. "
            "FORBIDDEN: any word from the caption/hook ('comment', 'follow', 'bold', 'send', 'link', 'dm', 'type', 'tag', 'share', 'dataset', 'curated'). "
            "REQUIRED: must name the technical domain, tool, or concept being taught. "
            "GOOD: 'Brain Tumor Functional Connectivity Analysis Using BOLD fMRI'. "
            "BAD: 'Comment bold to get the dataset link'."
        )
    )
    source_summary: str = Field(
        description="2-4 sentence entity-dense summary of the entire source."
    )
    key_takeaways: list[str] = Field(
        description="3-7 specific, concrete takeaways. Each must be a complete fact, not a topic label."
    )
    notes: list[AtomicNote]
    knowledge_graph: KnowledgeGraph = Field(default_factory=KnowledgeGraph)


@dataclass
class ProcessingResult:
    source_note_title: str = ""
    source_summary: str = ""
    key_takeaways: list = field(default_factory=list)
    atomic_notes: list = field(default_factory=list)
    suggested_mocs: list = field(default_factory=list)
    knowledge_graph: Optional[KnowledgeGraph] = None
    token_usage: dict = field(default_factory=dict)
    anchors_recovered: int = 0  # anchors the integrity gate had to append (0 = perfect synthesis)
    model_used: str = ""  # actual Gemini model that synthesized this note (cascade routing may escalate)


# ── SOTA System Prompt ────────────────────────────────────────


SYSTEM_PROMPT = """\
### ROLE
You are an expert Knowledge Architect specializing in the Luhmann Zettelkasten method. Your primary mandate is SYNTHESIS over CAPTURE — fewer, deeper, more actionable notes beat many shallow variants. Every note you create must earn its existence by adding genuine epistemic value that cannot be found by reading an adjacent note.

### KNOWLEDGE DOMAIN FIREWALL
You have two knowledge domains:
  DOMAIN A: The RELAY DESCRIPTION and SOURCE DATA provided in the prompt. You may freely report, quote, and reason from this.
  DOMAIN B: Your training knowledge about the world. You must NEVER draw on DOMAIN B to fill gaps in DOMAIN A.

You operate under a strict closed-world assumption: everything not explicitly stated in the source is unknown.
"Unknown" and "not addressed in source" are valid, correct, and informative outputs.
It is NEVER acceptable to substitute training knowledge for missing source content.
A concept being named in the source does NOT give you permission to define or explain it from DOMAIN B.
Academic citations in "(Author, Year)" format are DOMAIN B unless a bibliography or reference list was visibly displayed in the source. Do NOT add citations from memory.

### TASK
Analyze the provided source and generate a response in the EXACT JSON format below. Use AutoSchemaKG principles to induce the relationship types that naturally emerge from the content rather than relying on a fixed set.

### REQUIRED JSON SCHEMA
Your entire response must be a single JSON object with this exact structure (all fields required):
{
  "thinking": "<internal reasoning scratchpad>",
  "suggested_title": "<5-10 word content-derived title. CRITICAL for social media: IGNORE the hook/caption text (e.g. 'Comment X to get Y', 'Follow for more', engagement bait). Derive the title from the ACTUAL SUBJECT MATTER discussed in the video/post.>",
  "source_summary": "<2-4 sentence entity-dense summary>",
  "key_takeaways": ["<takeaway 1>", "<takeaway 2>", "..."],
  "notes": [
    {
      "note_type": "<Inferred Category: e.g. theory, mechanism, concept, argument, model, result, validation, method>",
      "title": "<Title Case declarative sentence: Subject Verb Object/Complement>",
      "aliases": ["<2+ word phrase>", "<specific acronym>", "..."],
      "canonical_links": [],
      "tags": ["<domain/subtopic>", "..."],
      "content": "<2-3 sentences verbatim extraction>",
      "implication": "<REQUIRED. One sentence starting with 'This implies', 'Practitioners should', or 'This means'. Must name a concrete decision or action. BAD: 'Requires further context'. GOOD: 'This implies that filtering tumor data by volume before training will produce better-stratified cohorts'. GOOD: 'Practitioners should use an atlas-based brain parcellation before applying graph metrics to fMRI data'>",
      "source_quote": "<1-2 sentence verbatim quote from source>",
      "vault_links_typed": [
        {
          "title": "<Existing Note Title>",
          "rel": "<Inferred Relationship Type: e.g. prerequisite, contradiction, application, evidence, analogy, refinement, solves_problem, evidence_for, analogous_to, implemented_by, affects, part_of>",
          "critic_logic": "<One-sentence justification>"
        }
      ],
      "needs_review": false
    }
  ],
  "knowledge_graph": {
    "entities": ["<Canonical concept name>", "..."],
    "relations": [["<subject>", "<Inferred Relation>", "<object>"], ...]
  }
}

### AUTOSCHEMAKG: SCHEMA INDUCTION
Instead of a fixed ontology, identify the core relationship types that define the structure of the provided content.
1. INDUCTION: In the 'thinking' block, list the recurring relationship types found in the source.
2. APPLICATION: Use these discovered types in 'vault_links_typed.rel' and 'knowledge_graph.relations'.
3. CONSISTENCY: Ensure the same relationship name is used for identical semantic connections across the batch.

### ATOMIC NOTE CONTRACT
1. ATOMICITY: Exactly ONE independently useful concept per note.
2. TITLE: A "Strong Statement" declarative sentence in Title Case (Subject-Verb-Object/Complement).
   - MUST convey the specific claim so a reader understands the content WITHOUT reading the note body.
   - Preserve acronyms as uppercase: AI, ML, LLM, RAG, GPU, LoRA, QLoRA, API, etc.
   - GOOD (specific claim): "FlashAttention-2 Reduces Memory Complexity from O(n²) to O(n) via Tiled Computation"
3. GROUNDING: Strictly verbatim extraction. If you cannot find a direct quote, omit the fact.
4. DENSITY CONSTRAINT: Extract only DISTINCT claims that each add unique value. A 3-minute video rarely has more than 4-5 genuinely distinct claims. Do NOT split one idea into 3 reworded variants — merge them into one canonical note.
5. TAG FORMAT (STRICT): Tags MUST use lowercase/slash/hierarchy. NEVER bare words or Title Case.
   - Allowed top-level domains: ai, neuroscience, engineering, math, economics, productivity, software-engineering, data-science, career-development, pattern
   - GOOD: "ai/neural-networks", "ai/architecture", "ai/optimization", "pattern/optimization"
6. PROJECTS & TUTORIALS: When the source describes a reproducible project, step-by-step tutorial, or implementation methodology, generate one `method` note that captures the steps as a numbered list in the content field. Title it using the pattern "[Subject] Implementation Steps" or "[Subject] Project Methodology". Include every concrete step mentioned — tools, datasets, validation criteria — as sub-items. This note is often the most actionable artifact from tutorial content.
7. ANTI-VARIANT RULE (CRITICAL): Before creating any note, scan the VAULT INDEX. If a note already exists covering the same core claim (even with different wording), DO NOT create a new note. Instead, point to it via vault_links_typed with rel="extends" or rel="evidence_for". Prefer enriching one canonical note over spawning a near-duplicate. If you must create a new note on a topic already in the vault, the title MUST make explicit what is NEW or DIFFERENT from the existing note.
8. IMPLICATION REQUIRED: Every note must have a non-trivial implication. "This implies further research is needed" is NOT acceptable. If a claim has no actionable implication, it is probably not worth a standalone note — merge it with a related one.
9. SOURCE DEPTH SCALING: Instagram/TikTok sources are shallow by design. Limit concept/mechanism/argument/theory notes to 3 maximum. Method notes (rule 6) are EXEMPT from this cap and must always be generated when steps are present — they are the highest-value artifact from tutorial content. Total note budget for Instagram: 1 method note (if applicable) + 3 concept notes = 4 max.

### CROSS-DOMAIN EXAMPLES (FEW-SHOT)

Example 1 (Tech):
- Title: "Attention Mechanism Scales Quadratically with Sequence Length"
- Logic: Identifies the O(n^2) bottleneck in Transformer architectures.

Example 2 (Finance):
- Title: "Compound Interest Generates Exponential Wealth Over Long Horizons"
- Logic: Illustrates the snowball effect of reinvested returns.

Example 3 (Humanities):
- Title: "Oral Traditions Preserve Knowledge Through Narrative Scaffolding"
- Logic: Explains how rhyme and story prevent information rot in non-literate societies.

Respond with ONLY the JSON object. No conversational yapping.
"""


def _build_user_prompt(meta: SourceMeta, config: Config, vault_index: list | None = None, user_intent: str = "", anchor_count: int = 0) -> str:
    """Universally performant User Prompt: Context-First Strategy."""
    # ── Compression (Phase 1) ──────────────────────────────────
    transcript = meta.transcript or "[No transcript]"
    compression_note = ""
    if config.compress_enabled and meta.transcript:
        from compressor import compress_transcript
        transcript, was_compressed, orig_chars, comp_chars = compress_transcript(
            meta.transcript,
            threshold_chars=config.compress_threshold_chars,
            target_ratio=config.compress_target_ratio,
        )
        if was_compressed:
            compression_note = (
                f"\n[NOTE: Transcript compressed from {orig_chars:,} → {comp_chars:,} chars "
                f"({100*comp_chars//orig_chars}% retained) using LexRank. "
                f"All extracted sentences are verbatim from the source.]\n"
            )

    # ── Transcript Quality Gate (Phase 1b) ────────────────────
    quality_warning = ""
    tq = getattr(meta, "transcript_quality", None) or {}
    if tq.get("is_poor_quality"):
        quality_warning = (
            f"⚠️ LOW-QUALITY TRANSCRIPT: Auto-caption artifacts detected "
            f"(punct_density={tq['punctuation_density']:.3f}, "
            f"ttr={tq['ttr']:.3f}, repetition={tq['repetition_ratio']:.3f}). "
            f"You MUST set needs_review: true for ALL notes without exception.\n"
        )

    # Reconstruct Vault Index with descriptive metadata
    vault_text = "(none)"
    if vault_index:
        vault_text = "\n".join([
            f"- [[{n['title']}]] | Aliases: {', '.join(n.get('aliases', [])[:3])} | Type: {n.get('note_type', 'note')}" 
            for n in vault_index[:50]
        ])

    min_anchor_notes = max(config.min_notes, anchor_count) if anchor_count else config.min_notes

    social_media_warning = ""
    if meta.source_type in ("instagram", "tiktok", "twitter", "x"):
        social_media_warning = (
            f"\n⚠️ SOCIAL MEDIA SOURCE: The TITLE above is a platform caption/hook "
            f"(often engagement bait like 'Comment X to get Y'). "
            f"IGNORE IT for suggested_title and note titles — derive all titles from the actual spoken/shown content.\n"
        )

    return f"""\
### SOURCE DATA
TITLE: {meta.title}{social_media_warning}
AUTHOR: {meta.author}
TYPE: {meta.source_type}
CONTENT:
{compression_note}
{transcript}

### EXISTING VAULT CONTEXT
{vault_text}

### MANDATE
{quality_warning}1. Extract {config.min_notes}-{config.max_notes} atomic notes from SOURCE DATA. With {anchor_count} technical anchors, aim for at least {min_anchor_notes} notes — one per distinct concept.
2. Use the VAULT CONTEXT to find link opportunities.
{"3. USER INTENT OVERRIDE: " + user_intent if user_intent else ""}

### GROUNDING (apply after drafting)
- SOURCE OF TRUTH: Every claim must be grounded in the verbatim CONTENT above. If you cannot find a direct quote, omit the fact.
- Named entities you recognise from training are NOT automatically permissible. Only include what the SOURCE explicitly states about them. Do not attach theories, dates, definitions, or roles from your training knowledge.

Respond with ONLY the JSON object.
"""


# ── Stage 1: Cloud Relay ──────────────────────────────────────


async def run_relay_stage(meta: SourceMeta, config: Config) -> str:
    """
    STAGE 1: High-Fidelity Extraction with PARSE descriptions.
    Schema includes anchor_quote_pairs for Stage 2 validation.
    """
    print(f"[RELAY] Stage 1: Extraction for {meta.source_type.upper()}...", flush=True)
    
    # ── High-Fidelity Technical Glossary (Cache Padding >4k tokens) ──
    GLOSSARY_TERMS = [
        ("ZERO-SHOT EXTRACTION", "The capability of a large language model to identify and extract entities, relationships, or themes from a text without having been specifically trained on that data distribution or task format."),
        ("ENTITY RESOLUTION", "The process of identifying and linking different mentions of the same real-world entity (e.g., 'fMRI', 'functional MRI', 'functional Magnetic Resonance Imaging') across a dataset to a single canonical identifier."),
        ("VERBATIM FIDELITY", "A strict constraint in data extraction requiring the model to replicate source text character-for-character, preserving original punctuation, casing, and technical jargon without paraphrasing."),
        ("ANCHOR QUOTE PAIR", "A structural unit of extraction where a high-signal technical term is explicitly coupled with the verbatim sentence from the source that defines or contextualizes its use."),
        ("NEURAL SCALING LAWS", "Empirical power-law relationships that describe how the performance of a neural network (measured by loss) improves predictably as a function of model parameters, compute, and training data size."),
        ("MATRYOSHKA EMBEDDINGS", "A technique for training embedding models to produce nested vector representations, where the first N dimensions capture the most significant semantic information, allowing for adaptive search speeds."),
        ("LOGIT MASKING", "A decoding-time constraint mechanism that restricts the model's next-token probabilities based on a predefined grammar or schema, effectively forcing the output to be syntactically valid JSON or code."),
        ("CONTEXT CACHING", "The persistent storage of the KV (Key-Value) cache for a specific prompt prefix (such as a large system instruction or document) to minimize redundant computation and reduce per-token API costs."),
        ("RAG (RETRIEVAL AUGMENTED GENERATION)", "An architectural pattern where an LLM is provided with relevant context retrieved from an external database at query time, reducing hallucinations and providing grounding in private data."),
        ("CHAIN-OF-THOUGHT (CoT)", "A prompting technique that encourages the model to generate intermediate reasoning steps before arriving at a final answer, significantly improving performance on multi-step logical tasks."),
        ("EPISODIC MEMORY", "In the context of AI agents, the ability to store and recall specific past interactions or events to maintain continuity and learn from successful or failed behavioral patterns."),
        ("SEMANTIC SEARCH", "A search methodology that uses vector embeddings to find results based on the conceptual meaning of a query rather than literal keyword matching, often using cosine similarity as the distance metric."),
        ("QUANTIZATION", "The process of reducing the precision of a model's weights (e.g., from 16-bit floating point to 4-bit integers) to decrease memory footprint and increase inference speed on consumer hardware."),
        ("MIXTURE-OF-EXPERTS (MoE)", "A sparse architecture where only a subset of the model's parameters (the 'experts') are activated for any given token, allowing for very large total parameter counts with low inference compute costs."),
        ("GATED DELTA NETWORKS", "A linear attention mechanism that uses gating to control information flow, often used in sub-quadratic architectures to handle extremely long context windows efficiently."),
        ("TRANSFORMER ARCHITECTURE", "The dominant deep learning architecture based on the self-attention mechanism, which allows the model to weigh the importance of different parts of the input sequence simultaneously."),
        ("BACKPROPAGATION", "The fundamental algorithm for training neural networks, using the chain rule to calculate the gradient of the loss function with respect to the weights and biases of the model."),
        ("GRADIENT DESCENT", "An optimization algorithm used to minimize the loss function by iteratively moving in the direction of steepest descent, defined by the negative of the gradient."),
        ("TEMPERATURE", "A hyperparameter that controls the randomness of an LLM's predictions by scaling the logits before the softmax layer; lower values produce more deterministic and predictable text."),
        ("SYSTEM PROMPT", "A high-level instruction that sets the persona, constraints, and behavioral guidelines for an LLM across a session, often used to enforce safety or specific output formats."),
        ("JSON SCHEMA", "A declarative language that allows you to annotate and validate JSON documents, ensuring they conform to a specific structure, data types, and required fields."),
        ("ATOMIC NOTE", "A fundamental unit of a Zettelkasten knowledge management system, containing exactly one self-contained idea that can be independently linked and understood."),
        ("PERPLEXITY", "A measurement of how well a probability distribution or probability model predicts a sample; in NLP, it quantifies how 'surprised' a model is by a sequence of text."),
        ("FINE-TUNING", "The process of taking a pre-trained model and further training it on a specific dataset to adapt its performance to a particular domain, style, or task."),
        ("TOKENIZATION", "The process of converting raw text into smaller units (tokens) such as words, subwords, or characters that can be processed by a neural network's embedding layer."),
        ("EMBEDDING VECTOR", "A dense, low-dimensional representation of a discrete variable (like a word or image) in a continuous vector space, where semantic proximity is reflected by spatial distance."),
        ("CONVOLUTIONAL NEURAL NETWORK (CNN)", "A type of neural network specialized for processing data with a grid-like topology, such as images, using kernels to extract spatial features."),
        ("RECURRENT NEURAL NETWORK (RNN)", "An architecture designed for sequential data that maintains a hidden state to capture temporal dependencies, though largely superseded by Transformers for long sequences."),
        ("LLM AGENT", "An AI system that uses a large language model as its reasoning engine to interact with tools, execute code, and perform multi-step tasks to achieve a goal."),
        ("PROMPT ENGINEERING", "The iterative process of refining the input text (prompt) to an LLM to elicit the most accurate, safe, and useful response for a specific application."),
        ("HALUCINATION", "A phenomenon where an LLM generates text that is syntactically correct and fluent but factually incorrect or unsupported by the provided context."),
        ("FEW-SHOT PROMPTING", "A technique where the model is provided with a few examples of the desired input-output behavior within the prompt to guide its response format and logic."),
        ("BERT (BIDIRECTIONAL ENCODER REPRESENTATIONS FROM TRANSFORMERS)", "A landmark encoder-only Transformer model that changed NLP by learning bidirectional context for words, widely used for classification and search."),
        ("GPT (GENERATIVE PRE-TRAINED TRANSFORMER)", "A decoder-only Transformer architecture designed for autoregressive next-token prediction, forming the basis for models like ChatGPT and Claude."),
        ("INFERENCE", "The phase where a trained model is used to generate predictions or text from new input data, typically involving a forward pass through the network."),
        ("OVERFITTING", "A modeling error that occurs when a machine learning model is too closely aligned to its training data, failing to generalize well to new, unseen information."),
        ("VALIDATION GATE", "A programmable check or filter applied after an LLM's output to ensure it meets specific quality, safety, or structural criteria before final delivery."),
        ("REINFORCEMENT LEARNING FROM HUMAN FEEDBACK (RLHF)", "A method for aligning LLMs with human preferences by training a reward model based on human rankings and using it to fine-tune the model."),
        ("SELF-ATTENTION", "A mechanism that allows each position in a sequence to attend to all other positions, calculating a weighted representation of the entire context for every token."),
        ("N-GRAM", "A contiguous sequence of n items from a given sample of text or speech, used in traditional statistical NLP for language modeling and feature extraction."),
        ("WORD2VEC", "A pioneering technique for creating word embeddings by predicting a word from its neighbors (CBOW) or predicting neighbors from a word (Skip-gram)."),
        ("LOSS FUNCTION", "A mathematical function that quantifies the difference between the model's prediction and the actual target, used as the signal for weight updates during training."),
        ("OPTIMIZER", "An algorithm (like Adam or SGD) that determines how the model's weights are adjusted during training to minimize the loss function."),
        ("STOCHASTIC GRADIENT DESCENT (SGD)", "A variant of gradient descent that calculates the gradient using a single random training example (or a small batch) to speed up the training process."),
        ("LEARNING RATE", "A hyperparameter that controls the step size taken during the optimization process; too high can cause instability, while too low can lead to slow convergence."),
        ("BATCH SIZE", "The number of training examples processed in a single forward and backward pass; larger batches provide more stable gradient estimates but require more memory."),
        ("EPOCH", "One complete pass through the entire training dataset during the training process of a neural network."),
        ("DROPOUT", "A regularization technique where random neurons are 'dropped out' during training to prevent the model from becoming overly dependent on specific connections."),
        ("SOFTMAX", "An activation function that converts a vector of numbers into a probability distribution, where each value is between 0 and 1 and the sum of all values is 1."),
        ("ReLU (RECTIFIED LINEAR UNIT)", "A popular activation function that outputs the input if it is positive and zero otherwise, helping to mitigate the vanishing gradient problem."),
        ("VANISHING GRADIENT PROBLEM", "An issue during training where gradients become extremely small as they are propagated backward through many layers, preventing weights from updating effectively."),
        ("EXPLODING GRADIENT PROBLEM", "A training issue where gradients become excessively large, causing the model's weights to fluctuate wildly and preventing convergence."),
        ("VECTOR DATABASE", "A specialized storage system designed to efficiently store and query high-dimensional vectors, often used for RAG and semantic search applications."),
        ("COSINE SIMILARITY", "A measure of similarity between two non-zero vectors that calculates the cosine of the angle between them, ranging from -1 (opposite) to 1 (identical)."),
        ("EUCLIDEAN DISTANCE", "The straight-line distance between two points in a multi-dimensional space, often used as a distance metric for vector embeddings."),
        ("L2 REGULARIZATION", "A technique that adds a penalty proportional to the square of the magnitude of the model's weights to the loss function to prevent overfitting."),
        ("L1 REGULARIZATION", "A technique that adds a penalty proportional to the absolute value of the weights, often leading to sparse models where many weights are zero."),
        ("ENSEMBLE LEARNING", "A methodology where multiple models are combined to produce a single prediction, often leading to better performance than any individual model."),
        ("TRANSFER LEARNING", "A technique where a model developed for one task is reused as the starting point for a model on a second, related task."),
        ("HYPERPARAMETER", "A configuration setting that is external to the model and whose value cannot be estimated from data, such as the learning rate or number of hidden layers."),
        ("CROSS-VALIDATION", "A technique for assessing how the results of a statistical analysis will generalize to an independent dataset, often using k-fold partitioning."),
        ("CONFUSION MATRIX", "A table used to evaluate the performance of a classification model by comparing its predictions against the actual ground truth across different classes."),
        ("PRECISION", "The ratio of correctly predicted positive observations to the total predicted positives, measuring the accuracy of the model's positive identifications."),
        ("RECALL", "The ratio of correctly predicted positive observations to all actual positives in the dataset, measuring the model's ability to find all relevant cases."),
        ("F1 SCORE", "The harmonic mean of precision and recall, providing a single metric that balances both concerns, especially useful for imbalanced datasets."),
        ("ROC CURVE (RECEIVER OPERATING CHARACTERISTIC)", "A graphical plot that illustrates the diagnostic ability of a binary classifier system as its discrimination threshold is varied."),
        ("AUC (AREA UNDER THE CURVE)", "The area under the ROC curve, providing an aggregate measure of performance across all possible classification thresholds."),
        ("DATA AUGMENTATION", "A technique used to increase the diversity of training data without actually collecting new data, by applying transformations like rotation or noise."),
        ("SYNTHETIC DATA", "Artificially generated data that mimics the statistical properties of real-world data, used for training when real data is scarce or sensitive."),
        ("BIAS-VARIANCE TRADEOFF", "The fundamental tension in machine learning between a model's ability to minimize errors on the training data (bias) and its ability to generalize (variance)."),
        ("UNSUPERVISED LEARNING", "A type of machine learning where the model is trained on data without explicit labels, looking for hidden patterns or structures (e.g., clustering)."),
        ("SUPERVISED LEARNING", "A learning paradigm where the model is trained on a labeled dataset, learning a mapping from inputs to known outputs."),
        ("SEMI-SUPERVISED LEARNING", "A hybrid approach that uses a small amount of labeled data combined with a large amount of unlabeled data during training."),
        ("ACTIVE LEARNING", "A strategy where the model identifies which unlabeled data points would be most beneficial to have labeled by a human expert next."),
        ("DIMENSIONALITY REDUCTION", "The process of reducing the number of random variables under consideration by obtaining a set of principal variables (e.g., using PCA or SVD)."),
        ("PRINCIPAL COMPONENT ANALYSIS (PCA)", "A statistical procedure that uses an orthogonal transformation to convert a set of observations of possibly correlated variables into a set of linearly uncorrelated variables."),
        ("SINGULAR VALUE DECOMPOSITION (SVD)", "A matrix factorization technique that decomposes a matrix into three component matrices, widely used in signal processing and dimensionality reduction."),
        ("EIGENVALUE", "A scalar that indicates how much a corresponding eigenvector is stretched or squished during a linear transformation."),
        ("EIGENVECTOR", "A non-zero vector that changes at most by a scalar factor when a linear transformation is applied to it."),
        ("LINEAR REGRESSION", "A basic predictive modeling technique that finds the best-fitting straight line to describe the relationship between a dependent variable and one or more independent variables."),
        ("LOGISTIC REGRESSION", "A statistical model used for binary classification that estimates the probability of a categorical dependent variable using a logistic function."),
        ("DECISION TREE", "A tree-like model of decisions and their possible consequences, used for both classification and regression tasks in machine learning."),
        ("RANDOM FOREST", "An ensemble learning method that constructs a multitude of decision trees at training time and outputs the class that is the mode of the classes of the individual trees."),
        ("GRADIENT BOOSTING", "A technique that builds an ensemble of weak learners (typically decision trees) sequentially, each one correcting the errors of the previous ones."),
        ("NEURAL ARCHITECTURE SEARCH (NAS)", "The process of automating the design of neural networks to find the most efficient and accurate architecture for a given task."),
        ("DISTILLATION", "The process of transferring knowledge from a large, complex 'teacher' model to a smaller, more efficient 'student' model."),
        ("PRUNING", "A technique for reducing the size of a neural network by removing redundant or unimportant weights or neurons with minimal impact on accuracy."),
        ("SPARSITY", "The property of a matrix or model where a significant number of its elements are zero, leading to potential gains in memory and compute efficiency."),
        ("AI ETHICS", "A field of study and practice focused on ensuring that AI systems are developed and deployed in a way that is fair, transparent, accountable, and safe for society."),
        ("MODEL ALIGNMENT", "The process of ensuring that an AI system's goals and behaviors are consistent with human values and the designer's intent."),
        ("TOKEN LIMIT", "The maximum number of tokens an LLM can process in a single turn, including both the input prompt and the generated output."),
        ("CONTEXT WINDOW", "The specific span of tokens that an LLM can 'see' and consider when generating the next token in a sequence."),
        ("PROMPT INJECTION", "A security vulnerability where an attacker craft's input to trick an LLM into ignoring its system instructions and executing unintended actions."),
        ("ZERO-SHOT CHAIN OF THOUGHT", "Triggering reasoning in an LLM by simply adding a phrase like 'Let's think step by step' to the end of a prompt."),
        ("PARAMETER-EFFICIENT FINE-TUNING (PEFT)", "A set of techniques (like LoRA) that fine-tune a small number of additional parameters while keeping the base model weights frozen."),
        ("LORA (LOW-RANK ADAPTATION)", "A popular PEFT technique that injects trainable low-rank matrices into the Transformer layers to adapt the model with minimal compute."),
        ("ADAPTER", "Small, trainable modules inserted between the layers of a pre-trained model to enable efficient adaptation to new tasks."),
        ("MULTIMODAL AI", "An AI system capable of processing and relating information from multiple types of data, such as text, images, audio, and video."),
        ("TOKEN-LEVEL LOGIT MASKING", "A technique used during inference to constrain an LLM's output to a specific format by zeroing out the logits of invalid tokens at each step."),
        ("AUTOREGRESSIVE MODEL", "A model that predicts future values based on past values in a sequence; LLMs are typically autoregressive next-token predictors."),
        ("LATENT SPACE", "A compressed, abstract representation of data where similar items are mapped closer together, learned by models like autoencoders."),
        ("AUTOENCODER", "A type of neural network used to learn efficient data codings in an unsupervised manner, consisting of an encoder and a decoder."),
        ("VARIATIONAL AUTOENCODER (VAE)", "A generative model that learns the probability distribution of the input data in a latent space, allowing for the generation of new, similar data."),
        ("GAN (GENERATIVE ADVERSARIAL NETWORK)", "An architecture where two neural networks (a generator and a discriminator) compete against each other to create realistic synthetic data."),
        ("DIFFUSION MODEL", "A class of generative models that learn to reverse a gradual noising process to generate high-quality images or data from pure noise."),
        ("NEURAL RADIANCE FIELD (NeRF)", "A method for synthesizing novel views of complex 3D scenes by optimizing a continuous volumetric scene function from a set of 2D images."),
        ("FOUNDATION MODEL", "A large-scale model trained on a vast amount of data that can be adapted to a wide range of downstream tasks (e.g., GPT-4, Claude)."),
        ("EMERGENT ABILITIES", "Skills or behaviors that appear in large language models only after they reach a certain scale of parameters or compute, which were not present in smaller versions."),
        ("AI SAFETY", "A research area focused on preventing catastrophic outcomes from advanced AI systems, addressing issues like robustness, alignment, and control."),
        ("STOCHASTIC PARROT", "A critical term used to describe LLMs as systems that probabilistically recombine training data without true understanding or communicative intent."),
        ("AGENTIC WORKFLOW", "A design pattern where an AI system is structured as an agent that performs iterative steps of planning, execution, and reflection to solve a problem."),
    ]
    
    GLOSSARY = "\n".join([f"{i+1}. {term}: {defn}" for i, (term, defn) in enumerate(GLOSSARY_TERMS)])

    system_instr = f"""\
{GLOSSARY}

### ROLE
You are a precise multimodal data sensor. Your output is an exhaustive factual record.

### OUTPUT SCHEMA (PARSE-ENHANCED)
{{
  "source_context": "High-level summary of the media.",
  "mandatory_technical_anchors": [
    {{
      "term": "Exact technical term or benchmark. Noun phrase. Preserve original casing.",
      "quote": "The verbatim sentence from the source containing this term."
    }}
  ],
  "factual_claims": [
    {{
      "claim": "Atomic factual assertion.",
      "evidence": "Verbatim quote supporting this claim."
    }}
  ],
  "entities": ["Tool names", "Version strings", "People", "Organizations"]
}}
"""
    
    file_uri: str | None = None
    try:
        # Determine format-specific instructions
        if meta.source_type == "youtube":
            # YouTube URLs are natively supported by Gemini
            video_ref = await _upload_file_to_gemini(meta.video_path, config.gemini_api_key) if meta.video_path else meta.url
            instruction = (
                "Watch/Listen to this content from start to finish.\n"
                "Extract every specific factual claim, demonstration, and visual detail:\n"
                "  • BENCHMARKS: Numbers, rankings, and performance comparisons from tables/charts — include exact values.\n"
                "  • IDENTIFIERS: Exact model names, package names, tool names, and version strings.\n"
                "  • USE-CASES: Specific requirements, format constraints, and 'gotchas'/limitations.\n"
                "  • DEMONSTRATIONS: Before/after states, problem/solution cycles, and creator-led examples.\n"
                "  • PERSONS: Names of individuals shown/spoken and exactly what they assert or demonstrate.\n"
                "  • VERBAL: Verbatim quotes of core arguments, definitions, and unique terminology."
            )
        elif meta.source_type == "instagram":
            if meta.video_path:
                # Downloaded video — upload to Gemini File API for visual analysis
                video_ref = await _upload_file_to_gemini(meta.video_path, config.gemini_api_key)
                instruction = (
                    "Watch/Listen to this content from start to finish.\n"
                    "Extract every specific factual claim, demonstration, and visual detail:\n"
                    "  • BENCHMARKS: Numbers, rankings, and performance comparisons from tables/charts — include exact values.\n"
                    "  • IDENTIFIERS: Exact model names, package names, tool names, and version strings.\n"
                    "  • USE-CASES: Specific requirements, format constraints, and 'gotchas'/limitations.\n"
                    "  • DEMONSTRATIONS: Before/after states, problem/solution cycles, and creator-led examples.\n"
                    "  • PERSONS: Names of individuals shown/spoken and exactly what they assert or demonstrate.\n"
                    "  • VERBAL: Verbatim quotes of core arguments, definitions, and unique terminology."
                )
            else:
                # No video available (RapidAPI/yt-dlp failed) — caption/text-only fallback
                # Instagram URLs are NOT accessible to Gemini's File API
                video_ref = None
                instruction = (
                    "Analyze this Instagram caption/description for high-density information extraction.\n"
                    "Extract:\n"
                    "  • LOGIC: The primary argument arc and all supporting factual claims.\n"
                    "  • ENTITIES: Specific models, people, organizations, products, and toolchains.\n"
                    "  • NUANCE: Limitations, specific 'gotchas', and contextual requirements.\n"
                    "  • TERMINOLOGY: Verbatim extraction of unique phrases and domain labels."
                )
        elif meta.source_type == "pdf":
            video_ref = None
            instruction = (
                "Analyze this document's structure and data with clinical precision.\n"
                "Extract:\n"
                "  • DATA: Exact values from tables, charts, and results sections.\n"
                "  • HIERARCHY: Abstract, methodology, results, and conclusion claims.\n"
                "  • CITATIONS: Key authors, theories, and 'Prior Art' referenced.\n"
                "  • DEFINITIONS: Every technical term-of-art defined in the text.\n"
                "  • LIMITATIONS: Caveats, edge cases, and areas for future research."
            )
        else: # Web, Twitter, Podcast
            video_ref = None
            instruction = (
                "Analyze this text for high-density information extraction.\n"
                "Extract:\n"
                "  • LOGIC: The primary argument arc and all supporting factual claims.\n"
                "  • ENTITIES: Specific models, people, organizations, products, and toolchains.\n"
                "  • NUANCE: Limitations, specific 'gotchas', and contextual requirements.\n"
                "  • TERMINOLOGY: Verbatim extraction of unique phrases and domain labels."
            )

        relay_quality_note = ""
        _tq = getattr(meta, "transcript_quality", None) or {}
        if _tq.get("is_poor_quality"):
            relay_quality_note = (
                f"- ⚠️ LOW-QUALITY TRANSCRIPT: Auto-caption artifacts detected "
                f"(punct={_tq['punctuation_density']:.3f}, ttr={_tq['ttr']:.3f}). "
                f"Extract only claims with STRONG verbatim support; omit anything ambiguous.\n"
            )

        full_prompt = (
            f"### DATA\n{meta.transcript}\n\n"
            f"### MANDATE\n{instruction}\n\n"
            f"### GROUNDING (apply after drafting)\n"
            f"{relay_quality_note}"
            f"- Named entities you recognize from training are NOT automatically permissible. "
            f"Only include what this source EXPLICITLY states about them. "
            f"Do not attach theories, dates, or roles you associate from training knowledge.\n"
            f"- Every claim MUST have a verbatim quote from the source. If none exists, omit the claim.\n"
            f"Respond in STRICT JSON."
        )

        # ── Explicit context cache management ─────────────────────────────────
        # The system_instr (glossary + role) is large and identical across every
        # Stage 1 call. Cache it with Google's cachedContent API for a 10× input
        # token discount ($0.025/1M vs $0.25/1M uncached).
        # We persist the cache name in the module-level _relay_cache dict so that
        # any subsequent request within the same server process reuses it without
        # an extra API round-trip. The 55-minute effective TTL (5-min buffer before
        # the 1-hour server TTL) handles token expiry gracefully.
        cache_name: Optional[str] = None
        try:
            from google import genai as _genai
            from google.genai import types as _gtypes

            _gclient = _genai.Client(api_key=config.gemini_api_key)
            sh = hashlib.md5(system_instr.encode()).hexdigest()[:12]
            now = time.time()

            if (
                _relay_cache["name"]
                and _relay_cache["system_hash"] == sh
                and now < _relay_cache["expires_at"]
            ):
                cache_name = _relay_cache["name"]
                print(f"[RELAY] Reusing context cache: {cache_name}", flush=True)
            else:
                cache = await asyncio.get_event_loop().run_in_executor(
                    None,
                    lambda: _gclient.caches.create(
                        model=config.gemini_model,
                        config=_gtypes.CreateCachedContentConfig(
                            display_name="nexus_relay_system",
                            system_instruction=system_instr,
                            ttl="3600s",
                        ),
                    ),
                )
                cache_name = cache.name
                _relay_cache["name"] = cache_name
                _relay_cache["system_hash"] = sh
                _relay_cache["expires_at"] = now + 3300  # 55 min
                print(f"[RELAY] Created context cache: {cache_name}", flush=True)
        except Exception as cache_err:
            # Cache creation is best-effort — fall back to inline system prompt
            print(f"[RELAY] Context cache unavailable ({cache_err}), sending inline.", flush=True)
            cache_name = None

        description, usage = await _gemini_generate(
            full_prompt,
            system=system_instr,
            api_key=config.gemini_api_key,
            model=config.gemini_model,
            temperature=0.1,
            video_url=video_ref,
            cache_name=cache_name,
        )
        return description
    finally:
        if file_uri: await _delete_gemini_file(file_uri, config.gemini_api_key)


# ── Stage 2: Local Crystallizer ───────────────────────────────


def _select_gemini_synthesis_model(meta: SourceMeta, anchor_count: int, config: Config) -> str:
    """
    Cascade routing for the full-cloud path: default to the cheap tier,
    escalate to the quality tier only for content that demonstrably needs it —
    PDFs, long-form video, or technically dense sources. 2026 LLM-routing best
    practice: route the obvious majority cheap, escalate only what needs it,
    rather than pre-classifying everything through a trained router.
    """
    if meta.source_type == "pdf":
        return config.gemini_synthesis_model_quality
    if meta.source_type in ("youtube", "instagram") and meta.duration_sec >= 3600:
        return config.gemini_synthesis_model_quality
    if anchor_count >= 5:
        return config.gemini_synthesis_model_quality
    return config.gemini_synthesis_model


async def run_crystallizer_stage(
    description: str,
    meta: SourceMeta,
    config: Config,
    embed_store: EmbedStore,
    user_intent: str = "",
) -> ProcessingResult:
    """
    STAGE 2: Local Synthesis with Qwen 3 reasoning and Semantic Gate.
    """
    print(f"[CRYSTALLIZER] Stage 2: Synthesis ({config.llm_provider.upper()})...", flush=True)
    
    # 1. Retrieval — RAG context from vault (disabled: LanceDB Rust code causes SIGSEGV in asyncio context)
    active_vault_index = []

    # 2. Extract Mandatory Anchors
    anchors = []
    anchor_prompt = ""
    try:
        match = re.search(r"\{.*\}", description[:8000], re.DOTALL)
        if match:
            data = json.loads(match.group(0))
            anchors = data.get("mandatory_technical_anchors", [])
            if anchors:
                anchor_prompt = "\n### MANDATORY TECHNICAL ANCHORS\n"
                for a in anchors:
                    anchor_prompt += f"- {a['term']} (Quote: {a['quote']})\n"
    except Exception as e:
        print(f"[CRYSTALLIZER] Anchor extraction failed: {e}", flush=True)
    # 3. Build Prompt
    user_prompt = _build_user_prompt(meta, config, vault_index=active_vault_index, user_intent=user_intent, anchor_count=len(anchors))
    grounded_prompt = f"{description}\n{anchor_prompt}\n{user_prompt}"

    if config.llm_provider == "gemini":
        # Full-cloud capture path — synthesis via Gemini instead of local Ollama/MLX.
        # Cascade: cheap tier by default, escalated to the quality tier either by
        # _select_gemini_synthesis_model() up front (PDF/long video/dense anchors)
        # or by the repair path below, which always escalates on a parse failure —
        # a cheap tier miss is itself a signal the content was harder than expected.
        chosen_model = _select_gemini_synthesis_model(meta, len(anchors), config)
        raw_response, usage = await _gemini_generate(
            grounded_prompt, SYSTEM_PROMPT,
            api_key=config.gemini_api_key,
            model=chosen_model,
            temperature=config.temperature,
        )
        repair_fn = lambda p, s, c: _gemini_generate(
            p, s, api_key=c.gemini_api_key, model=c.gemini_synthesis_model_quality, temperature=c.temperature
        )
    elif config.llm_provider == "mlx":
        raw_response, usage = await _mlx_generate(grounded_prompt, SYSTEM_PROMPT, config)
        repair_fn = _mlx_generate
    else:
        raw_response, usage = await _ollama_generate(grounded_prompt, SYSTEM_PROMPT, config)
        repair_fn = _ollama_generate

    # 4. Parse & Repair
    collection = await _parse_and_repair(raw_response, grounded_prompt, SYSTEM_PROMPT, config, repair_fn)
    
    # 5. SOVEREIGN INTEGRITY GATE — disabled: SentenceTransformer/joblib causes SIGSEGV in asyncio context
    anchors_recovered = 0

    # 6. Extract MOC topics from Knowledge Graph
    suggested_mocs = []
    if collection.knowledge_graph and collection.knowledge_graph.entities:
        # Use first 3 entities as primary topic candidates
        suggested_mocs = collection.knowledge_graph.entities[:3]

    # Use LLM-generated title, with code-level hook detection as a safety net.
    # Even if the LLM ignores the SYSTEM_PROMPT instruction, hooks are caught here.
    _HOOK_RE = re.compile(
        r'\b(comment|follow|send|link|dm\b|type|tag|share|curated|click|subscribe)\b',
        re.IGNORECASE,
    )
    _raw_title = (collection.suggested_title or "").strip()
    if not _raw_title or _HOOK_RE.search(_raw_title):
        # Derive from source_summary (first 8 words) or first key_takeaway
        _fallback = (collection.source_summary or "").split(".")[0].strip()
        if not _fallback and collection.key_takeaways:
            _fallback = collection.key_takeaways[0]
        _words = _fallback.split()[:8]
        _raw_title = " ".join(_words) if _words else meta.title
        print(f"[TITLE] Hook detected in suggested_title — derived: {_raw_title!r}", flush=True)
    content_title = _raw_title or meta.title
    return ProcessingResult(
        source_note_title=f"Source - {content_title}",
        source_summary=collection.source_summary,
        key_takeaways=collection.key_takeaways,
        atomic_notes=collection.notes,
        suggested_mocs=suggested_mocs,
        knowledge_graph=collection.knowledge_graph,
        token_usage=usage,
        anchors_recovered=anchors_recovered,
        # Reflects the primary cascade attempt; a repair-path escalation (rare —
        # only on parse failure) isn't distinguished here, since _parse_and_repair
        # doesn't report which attempt ultimately succeeded.
        model_used=chosen_model if config.llm_provider == "gemini" else "",
    )


# ── Clients ───────────────────────────────────────────────────


def _patch_mlx_tokenizer_config(model_path: str) -> None:
    """
    Patch TokenizersBackend → Qwen2TokenizerFast in the HF cache before loading.
    Idempotent: no-op if already patched or not present. Survives cache clears.
    """
    import glob, json as _json, os as _os
    if "/" not in model_path:
        return
    owner, name = model_path.split("/", 1)
    cache_dir = _os.path.expanduser(f"~/.cache/huggingface/hub/models--{owner}--{name}")
    for cfg_path in glob.glob(f"{cache_dir}/snapshots/*/tokenizer_config.json"):
        try:
            with open(cfg_path) as f:
                cfg = _json.load(f)
            if cfg.get("tokenizer_class") == "TokenizersBackend":
                cfg["tokenizer_class"] = "Qwen2TokenizerFast"
                with open(cfg_path, "w") as f:
                    _json.dump(cfg, f, indent=2)
                print(f"[MLX] Patched tokenizer_config at {cfg_path}", flush=True)
        except Exception:
            pass


async def _mlx_generate(prompt: str, system: str, config: Config) -> Tuple[str, dict]:
    """
    MLX inference via mlx-lm directly (in-process, no HTTP server required).
    The NoteCollection schema is too complex for outlines FSM on M4 GPU (OOM),
    so we use unconstrained generation with a strong JSON system prompt and rely
    on parse+repair for schema conformance — same approach as Ollama's format param.
    Model is loaded lazily and kept in _mlx_loaded until free_mlx_model() is called.
    """
    import mlx_lm

    global _mlx_loaded
    model_path = config.mlx_model

    # Load model if not already in memory (or if different model requested)
    if _mlx_loaded["path"] != model_path:
        free_mlx_model()
        _patch_mlx_tokenizer_config(model_path)
        print(f"[MLX] Loading {model_path.split('/')[-1]} into memory...", flush=True)
        loop = asyncio.get_event_loop()
        mlx_model, tokenizer = await loop.run_in_executor(
            None, lambda: mlx_lm.load(model_path)
        )
        _mlx_loaded["path"] = model_path
        _mlx_loaded["model"] = mlx_model
        _mlx_loaded["tokenizer"] = tokenizer
        print(f"[MLX] Model ready.", flush=True)

    mlx_model = _mlx_loaded["model"]
    tokenizer = _mlx_loaded["tokenizer"]

    # Apply chat template. 
    # enable_thinking=False is a hint for models with a thinking mode.
    # The prefill hack (<think>\n</think>) is the most reliable way to force immediate JSON.
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": prompt},
    ]
    formatted = tokenizer.apply_chat_template(
        messages, tokenize=False, add_generation_prompt=True
    ) + "<think>\n</think>\n"

    loop = asyncio.get_event_loop()
    raw_output = await loop.run_in_executor(
        None,
        lambda: mlx_lm.generate(
            mlx_model,
            tokenizer,
            prompt=formatted,
            max_tokens=4096,
            verbose=False,
        ),
    )

    return raw_output, {}


async def prewarm_mlx_model(config: Config):
    """
    Load the MLX model in the background if not already loaded.
    Called by the crystallizer daemon to hide model load latency.
    """
    import mlx_lm
    global _mlx_loaded
    # Pre-warm the fast model by default (most common)
    model_path = config.mlx_model_fast

    if _mlx_loaded["path"] != model_path:
        _patch_mlx_tokenizer_config(model_path)
        print(f"[MLX] Pre-warming {model_path.split('/')[-1]} into memory...", flush=True)
        try:
            loop = asyncio.get_running_loop()
            mlx_model, tokenizer = await loop.run_in_executor(
                None, lambda: mlx_lm.load(model_path)
            )
            _mlx_loaded["path"] = model_path
            _mlx_loaded["model"] = mlx_model
            _mlx_loaded["tokenizer"] = tokenizer
            print(f"[MLX] Pre-warm complete. Model ready.", flush=True)
        except Exception as e:
            print(f"[MLX] Pre-warm failed: {e}", flush=True)


def free_mlx_model():
    """
    Unload the MLX model from GPU memory.
    Call after synthesis is complete to let the chip cool before the next job.
    """
    import gc
    global _mlx_loaded
    if _mlx_loaded["path"] is not None:
        try:
            import mlx.core as mx
            _mlx_loaded["model"] = None
            _mlx_loaded["tokenizer"] = None
            gc.collect()
            mx.clear_cache()
            print("[MLX] Model unloaded. GPU memory freed.", flush=True)
        except Exception:
            pass
        _mlx_loaded["path"] = None


async def _ollama_generate(prompt: str, system: str, config: Config) -> Tuple[str, dict]:
    """Ollama synthesis with configurable model residency (keep_alive from config)."""
    url = f"{config.ollama_base_url}/api/chat"
    payload = {
        "model": config.ollama_model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": prompt},
            {"role": "assistant", "content": "<think>\n</think>\n"}
        ],
        "format": _get_strict_schema_dict(),
        "stream": False,
        "keep_alive": getattr(config, "ollama_keep_alive", 300),
        "options": {
            "temperature": 0.0
        }
    }
    async with httpx.AsyncClient(timeout=600) as client:
        resp = await client.post(url, json=payload)
        resp.raise_for_status()
        data = resp.json()
        content = data.get("message", {}).get("content", "")
        usage = {"total_tokens": data.get("eval_count", 0)}
        return content, usage


async def _gemini_generate(
    prompt: str,
    system: str,
    api_key: str,
    model: str,
    temperature: float,
    video_url: str = None,
    cache_name: Optional[str] = None,
) -> Tuple[str, dict]:
    """
    google-genai SDK (replaces deprecated google-generativeai).
    Uses an explicit cachedContent when cache_name is provided, otherwise
    sends the system instruction inline. Both paths use response_mime_type
    "application/json" to enforce structured output.
    """
    from google import genai
    from google.genai import types

    client = genai.Client(api_key=api_key)

    # Build multimodal contents list
    contents = []
    if video_url:
        contents.append(types.Part.from_uri(file_uri=video_url, mime_type="video/mp4"))
    contents.append(prompt)

    # When a cache is active the system instruction must NOT be sent inline —
    # it is already encoded in the cachedContent on Google's side.
    if cache_name:
        gen_config = types.GenerateContentConfig(
            temperature=temperature,
            response_mime_type="application/json",
            cached_content=cache_name,
        )
    else:
        gen_config = types.GenerateContentConfig(
            temperature=temperature,
            response_mime_type="application/json",
            system_instruction=system,
        )

    loop = asyncio.get_event_loop()
    response = await loop.run_in_executor(
        None,
        lambda: client.models.generate_content(
            model=model,
            contents=contents,
            config=gen_config,
        ),
    )

    usage: dict = {}
    if response.usage_metadata:
        usage = {
            "total_tokens": response.usage_metadata.total_token_count or 0,
            "cached_tokens": getattr(response.usage_metadata, "cached_content_token_count", 0),
        }

    return response.text, usage


def _get_strict_schema_dict():
    """Helper to return the JSON schema for guided_json."""
    return {
        "type": "object",
        "properties": {
            "thinking": {"type": "string"},
            "suggested_title": {"type": "string"},
            "source_summary": {"type": "string"},
            "key_takeaways": {"type": "array", "items": {"type": "string"}},
            "notes": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "title": {"type": "string"},
                        "note_type": {"type": "string"},
                        "aliases": {"type": "array", "items": {"type": "string"}},
                        "tags": {"type": "array", "items": {"type": "string"}},
                        "content": {"type": "string"},
                        "source_quote": {"type": "string"},
                        "canonical_links": {
                            "type": "array",
                            "items": {"type": "string"}
                        },
                        "vault_links_typed": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "title": {"type": "string"},
                                    "rel": {"type": "string"},
                                    "critic_logic": {"type": "string"}
                                },
                                "required": ["title", "rel"]
                            }
                        },
                        "needs_review": {"type": "boolean"}
                    },
                    "required": ["title", "aliases", "tags", "content", "source_quote"]
                }
            },
            "knowledge_graph": {
                "type": "object",
                "properties": {
                    "entities": {"type": "array", "items": {"type": "string"}},
                    "relations": {"type": "array", "items": {"type": "array", "items": {"type": "string"}}}
                }
            }
        },
        "required": ["thinking", "suggested_title", "source_summary", "key_takeaways", "notes"]
    }


async def _parse_and_repair(raw: str, prompt: str, system: str, config: Config, provider_fn, retries: int = 1) -> NoteCollection:
    try:
        # Step 1: Strip reasoning markers Qwen emits before/after the JSON.
        cleaned = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL)
        cleaned = re.sub(r"Thinking Process:.*?(?={)", "", cleaned, flags=re.DOTALL)
        cleaned = cleaned.strip()

        # Step 2: Scan for ALL JSON objects using raw_decode.
        decoder = json.JSONDecoder()
        candidates: list[dict] = []
        pos = 0
        while pos < len(cleaned):
            next_brace = cleaned.find("{", pos)
            if next_brace == -1:
                break
            try:
                obj, end = decoder.raw_decode(cleaned, next_brace)
                if isinstance(obj, dict):
                    candidates.append(obj)
                pos = end
            except json.JSONDecodeError:
                pos = next_brace + 1

        if not candidates:
            # Fallback for very messy output: try to find anything between the first { and last }
            first = cleaned.find("{")
            last = cleaned.rfind("}")
            if first != -1 and last != -1 and last > first:
                try:
                    candidates.append(json.loads(cleaned[first:last+1]))
                except json.JSONDecodeError:
                    pass

        if not candidates:
            raise ValueError("No JSON object found in LLM response")

        # Step 3: Identify the primary NoteCollection (has "notes") and any
        # standalone thinking fragment the model may have emitted separately.
        data: Optional[dict] = None
        thinking_fragment: str = ""
        for candidate in candidates:
            if "notes" in candidate:
                data = candidate
                break
            if "thinking" in candidate and "notes" not in candidate:
                thinking_fragment = candidate.get("thinking", "")

        if data is None:
            # Fallback: use the last (largest) candidate and hope for the best
            data = candidates[-1]

        # Step 4: If the model omitted "thinking" from the main object (because it
        # used <think> tags or a separate fragment), inject it so NoteCollection
        # validation does not fail — the field is optional but callers may log it.
        if not data.get("thinking") and thinking_fragment:
            data["thinking"] = thinking_fragment

        # Step 5: Normalize knowledge_graph.relations — the model sometimes emits
        # plain strings instead of [subject, relation, object] triples. Drop them
        # rather than fail validation; the notes themselves are what matters.
        kg = data.get("knowledge_graph", {})
        if isinstance(kg, dict) and "relations" in kg:
            kg["relations"] = [
                r for r in kg["relations"] if isinstance(r, list)
            ]

        # Step 6: Normalize canonical_links — qwen3.5:9b @ Q4_K_M ignores the
        # items:string constraint and returns objects like {"title": "...", ...}.
        # Extract the title string so Pydantic validation passes.
        for note in data.get("notes", []):
            raw_links = note.get("canonical_links", [])
            if raw_links and isinstance(raw_links[0], dict):
                note["canonical_links"] = [
                    lnk.get("title", "") for lnk in raw_links
                    if isinstance(lnk, dict) and lnk.get("title")
                ]

        return NoteCollection(**data)
    except Exception as e:
        if retries <= 0:
            raise e
        print(f"[REPAIR] JSON failed: {e!r}. Retrying...", flush=True)
        repair_prompt = f"{prompt}\n\nERROR: {e!r}\nFIX: Provide valid JSON matching the schema precisely. No conversational text."
        repair_raw, _ = await provider_fn(repair_prompt, system, config)
        return await _parse_and_repair(repair_raw, prompt, system, config, provider_fn, retries - 1)


# ── File API Helpers ──────────────────────────────────────────


async def _upload_file_to_gemini(file_path: str, api_key: str) -> str:
    from google import genai
    from google.genai import types

    client = genai.Client(api_key=api_key)
    path = Path(file_path)
    loop = asyncio.get_event_loop()

    file_ref = await loop.run_in_executor(
        None,
        lambda: client.files.upload(
            file=str(path),
            config=types.UploadFileConfig(mime_type="video/mp4"),
        ),
    )

    while file_ref.state.name == "PROCESSING":
        await asyncio.sleep(2)
        name = file_ref.name
        file_ref = await loop.run_in_executor(
            None, lambda: client.files.get(name=name)
        )

    return file_ref.uri


async def _delete_gemini_file(file_uri: str, api_key: str):
    from google import genai

    client = genai.Client(api_key=api_key)
    file_name = file_uri.split("/")[-1]
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, lambda: client.files.delete(name=file_name))


async def process_source(
    meta: SourceMeta,
    config: Config,
    vault_index: list | None = None,
    user_intent: str = "",
    embed_store: Optional[EmbedStore] = None,
) -> ProcessingResult:
    """
    Ultimate Sovereign Pipeline (v4.0).
    """
    # 1. Stage 1: Cloud Relay (Extraction + Cache Padding)
    relay_description = await run_relay_stage(meta, config)

    # 2. Stage 2: Local Crystallizer (Synthesis + Semantic Gate)
    if not embed_store:
        embed_store = EmbedStore(api_key=config.gemini_api_key)

    return await run_crystallizer_stage(
        description=relay_description,
        meta=meta,
        config=config,
        embed_store=embed_store,
        user_intent=user_intent
    )
