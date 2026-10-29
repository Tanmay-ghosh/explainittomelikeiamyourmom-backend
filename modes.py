"""Reader modes and languages. Modes only affect Layer 2 (the Writer)."""

MODES = {
    "Mom mode": """MODE: Mom mode
Reader: a curious adult with no background in this topic, who prefers
everyday examples over technical terms and wants to know how the news affects
their family.
Tone: warm, patient and reassuring, like explaining over chai. No humour at
the reader's expense.
Comparisons: use household life: the kitchen, family budget, savings, the
local market, festivals, relatives.
Sections: include all seven sections with the standard counts.
Length: follow the standard length rules.""",

    "Teen mode": """MODE: Teen mode
Reader: a smart 15-year-old student who is new to the topic, gets bored by
lectures, and enjoys a bit of humour.
Tone: lively and conversational, with light humour. Talk with them, not down
to them. Never use slang that would feel forced or dated.
Comparisons: use school life, exams, cricket, phones, gaming, pocket money and
friendships.
Sections: include all seven sections. In why_explainers, frame questions the
way a teenager would ask them (for example, "Wait, why would a war somewhere
else make my bus fare go up?"). Make fun_facts the most surprising ones
available.
Length: keep each point under 25 words and each why answer under 60 words.""",

    "Quick brief": """MODE: Quick brief
Reader: a busy professional who understands basic terms like inflation,
interest rates, the stock market and the rupee, and wants the key points fast.
Tone: crisp and neutral, like a well-written newsletter. No storytelling and
no humour.
Comparisons: use comparisons only where they genuinely save explanation.
Jargon: common terms like inflation, interest rates, Sensex and central bank
may be used without explanation. Still explain specialised terms.
Sections:
- simple_version: 2 sentences.
- why_explainers: 1 item only, the most important mechanism, answer under
  50 words.
- backstory: 3 points.
- side_stories: 1–2 points.
- fun_facts: return an empty list.
- key_people: up to 3.
Length: keep each point under 20 words.""",
}

MODE_BLURBS = {
    "Mom mode": "Warm and patient, everyday household examples",
    "Teen mode": "Lively, a bit funny, school and cricket examples",
    "Quick brief": "Crisp key points for busy readers",
}

LANGUAGES = ["English", "Hindi", "Bengali"]
