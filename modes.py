"""Class levels and languages. Levels only affect Layer 2 (the Writer).

The level names must match the frontend exactly (note the en dash "–").
"""

LEVELS = {
    "Std 5–6": """LEVEL: Std 5–6 (about 10 to 12 years old)
Reader: a curious student in Class 5 or 6 who reads well but has little
background knowledge.
Language: very short sentences (under 15 words). Everyday words only. If a
new word is truly needed, explain it in the same sentence.
Comparisons: home, school, playground, games, food, family, festivals.
Depth: the main idea and one clear reason why. Skip fine details.
Tone: warm, encouraging and simple, like a kind older sibling explaining.
Length: keep each point under 22 words and each "why" answer under 50 words.
Quiz: straightforward recall questions with clearly different options.""",

    "Std 7–8": """LEVEL: Std 7–8 (about 12 to 14 years old)
Reader: a student in Class 7 or 8 who knows the basics of science, geography
and civics from school.
Language: clear sentences (under 20 words). Introduce one or two real terms
when useful, always explained in the same sentence.
Comparisons: school life, cricket and other sports, phones, games, pocket
money, the neighbourhood.
Depth: cause and effect, and how the topic links to school subjects.
Tone: friendly and lively, never childish.
Length: keep each point under 28 words and each "why" answer under 65 words.
Quiz: mix recall and "why" questions.""",

    "Std 9–10": """LEVEL: Std 9–10 (about 14 to 16 years old)
Reader: a student in Class 9 or 10 preparing for board exams, able to handle
real terms and some nuance.
Language: proper terms are welcome (for example "monetary policy",
"photosynthesis", "federalism"), each explained briefly the first time.
Comparisons: use them only where they genuinely help.
Depth: fuller context, causes and consequences, and how the topic connects to
the syllabus and to current affairs.
Tone: respectful and direct, like a good teacher. Never talk down.
Length: keep each point under 32 words and each "why" answer under 80 words.
Quiz: include at least one question that tests understanding, not just recall.""",
}

LEVEL_BLURBS = {
    "Std 5–6": "Short sentences, everyday examples",
    "Std 7–8": "A bit more detail, links to school subjects",
    "Std 9–10": "Real terms explained, exam-ready context",
}

DEFAULT_LEVEL = "Std 7–8"

LANGUAGES = ["English", "Hindi", "Bengali"]
