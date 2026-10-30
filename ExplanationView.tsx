import { Link } from "@tanstack/react-router";
import {
  BookOpen, Building2, Check, ChevronLeft, ChevronRight, ExternalLink, HelpCircle, History,
  Lightbulb, Network, Sparkles, Trophy, User, Users, X,
} from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState, type MouseEvent, type ReactNode } from "react";
import { trackExplanationShown, trackFeedbackGiven, trackQuizAnswered, trackQuizCompleted, trackQuizStarted } from "@/lib/analytics";
import { nextLevel } from "@/lib/prefs";
import { subjectKey } from "@/lib/subject-art";
import type { Explanation, Language, Level, Story } from "@/lib/types";
import { cn } from "@/lib/utils";
import { SubjectTile, Tincu } from "./Art";
import { btn, Btn } from "./kit";
import { SourceBadge } from "./SourceBadge";
import { CategoryBadge, TopicTag } from "./StoryCard";

type Src = "weekly" | "bring_your_own";
type Tone = 1 | 2 | 3 | 4 | 5 | 6;
type Slide = { key: string; label: string; tone: Tone; icon: ReactNode; node: ReactNode; quiz?: boolean };

const TONE: Record<Tone, { bg: string; head: string; ink: string }> = {
  1: { bg: "bg-s1", head: "bg-s1-head", ink: "bg-primary text-primary-foreground" },
  2: { bg: "bg-s2", head: "bg-s2-head", ink: "bg-primary text-primary-foreground" },
  3: { bg: "bg-s3", head: "bg-s3-head", ink: "bg-coral text-foreground" },
  4: { bg: "bg-s4", head: "bg-s4-head", ink: "bg-teal text-foreground" },
  5: { bg: "bg-s5", head: "bg-s5-head", ink: "bg-card text-foreground" },
  6: { bg: "bg-s6", head: "bg-s6-head", ink: "bg-sun-deep text-foreground" },
};

const MESSAGES = [
  "No worries, this is tricky! Give the explanation another read.",
  "Good try! Read it once more and you'll get it.",
  "Nice work! You got most of it.",
  "Superstar! You really understood this one.",
];
function message(score: number, total: number) {
  if (total === 3) return MESSAGES[score];
  const r = score / total;
  return r === 1 ? MESSAGES[3] : r >= 0.6 ? MESSAGES[2] : r > 0 ? MESSAGES[1] : MESSAGES[0];
}

// Default picture symbols per subject, used when an explanation has no motifs of its own.
const DEFAULTS: Record<string, string[]> = {
  space: ["🚀", "🛰️", "🌕", "⭐", "🪐"],
  science: ["🔬", "🧪", "💡", "⚛️", "🔭"],
  biology: ["🧬", "🌿", "🦠", "🐾", "🫀"],
  mathematics: ["📐", "➗", "🔢", "📏", "🧮"],
  history: ["🏛️", "📜", "⏳", "🗿", "🏺"],
  geography: ["🗺️", "⛰️", "🌊", "🧭", "🏝️"],
  civics: ["🏛️", "⚖️", "🗳️", "📜", "🤝"],
  economics: ["💰", "📈", "🏦", "🪙", "📊"],
  environment: ["🌱", "☀️", "🌍", "💧", "🌳"],
  languages: ["📖", "✏️", "🗣️", "📝", "🔤"],
  computer: ["💻", "🤖", "🖱️", "⌨️", "📡"],
  "current-affairs": ["📰", "🌍", "🎙️", "🗞️", "📣"],
  sports: ["🏆", "🏅", "⚽", "🏏", "🎯"],
  arts: ["🎨", "🎭", "🎶", "🖌️", "🪔"],
  weather: ["🌀", "🌧️", "⛈️", "🌊", "☁️"],
  world: ["🌍", "🤝", "🕊️", "🗺️", "🏳️"],
  other: ["💡", "✨", "📚", "🔍", "🌟"],
};

/** Up to 5 short picture symbols for this explanation: its own motifs first, then the subject's defaults. */
/** Story symbols chosen by the writer AI; older explanations have none. */
const motifsOf = (e: Explanation) => (e as Explanation & { motifs?: string[] }).motifs;

function storyMotifs(motifs: string[] | undefined | null, subject?: string | null): string[] {
  const own = (motifs ?? []).map((m) => m.trim()).filter((m) => m.length > 0 && m.length <= 8);
  const fallback = DEFAULTS[subjectKey(subject)] ?? DEFAULTS.other;
  const out: string[] = [];
  for (const m of [...own, ...fallback]) if (!out.includes(m) && out.length < 5) out.push(m);
  return out;
}

const reducedMotion = () => typeof window !== "undefined" && window.matchMedia("(prefers-reduced-motion: reduce)").matches;

// Where the story's picture symbols sit in a card background: [left %, top %, size px, rotation deg].
// Mostly along the right edge and the bottom, where pages usually have empty space.
const LAYOUTS: [number, number, number, number][][] = [
  [[95, 7, 72, 12], [3, 52, 92, -12], [94, 62, 112, 8], [50, 96, 92, -6], [9, 93, 66, 10]],
  [[94, 30, 96, 10], [5, 9, 58, -8], [4, 80, 100, 8], [62, 97, 112, -10], [93, 91, 62, 14]],
  [[94, 20, 84, -6], [4, 36, 70, 12], [96, 80, 106, 10], [32, 97, 94, -12], [7, 94, 58, 6]],
];

/** Large, faint story symbols behind the slide content. Purely decorative. */
function Backdrop({ motifs, seed }: { motifs: string[]; seed: number }) {
  if (!motifs.length) return null;
  const layout = LAYOUTS[seed % LAYOUTS.length];
  return (
    <div className="slide-backdrop" aria-hidden>
      {layout.map(([left, top, size, rot], i) => (
        <span key={i} style={{ left: `${left}%`, top: `${top}%`, fontSize: size, transform: `translate(-50%, -50%) rotate(${rot}deg)` }}>
          {motifs[(i + seed) % motifs.length]}
        </span>
      ))}
    </div>
  );
}

/**
 * Slide body. Text starts at kid-friendly size; if a page would overflow, its text
 * shrinks a little (down to 78%) so it still fits. Scrolling stays as a last resort.
 */
function SlideBody({ children, tone, motifs, seed }: { children: ReactNode; tone: Tone; motifs: string[]; seed: number }) {
  const ref = useRef<HTMLDivElement>(null);
  const content = useRef<HTMLDivElement>(null);
  const [more, setMore] = useState(false);
  useEffect(() => {
    const el = ref.current;
    const inner = content.current;
    if (!el || !inner) return;
    let frame = 0;
    const fit = () => {
      let f = 1;
      el.style.setProperty("--fit", "1");
      while (el.scrollHeight > el.clientHeight + 1 && f > 0.79) {
        f = Math.round((f - 0.04) * 100) / 100;
        el.style.setProperty("--fit", String(f));
      }
      setMore(el.scrollHeight - el.clientHeight - el.scrollTop > 3);
    };
    const schedule = () => { cancelAnimationFrame(frame); frame = requestAnimationFrame(fit); };
    const resize = new ResizeObserver(schedule);
    const mutation = new MutationObserver(schedule);
    resize.observe(el);
    mutation.observe(inner, { childList: true, subtree: true, characterData: true });
    const onScroll = () => setMore(el.scrollHeight - el.clientHeight - el.scrollTop > 3);
    el.addEventListener("scroll", onScroll);
    void document.fonts?.ready.then(schedule);
    schedule();
    return () => { cancelAnimationFrame(frame); resize.disconnect(); mutation.disconnect(); el.removeEventListener("scroll", onScroll); };
  }, []);
  return <div className={cn("slide-body-wrap min-h-0 flex-1", TONE[tone].bg)}>
    <Backdrop motifs={motifs} seed={seed} />
    <div ref={ref} className="slide-body h-full overflow-y-auto" tabIndex={more ? 0 : undefined}>
      <div ref={content} className="slide-content">{children}</div>
    </div>
    {more && <div className="slide-more pointer-events-none" aria-hidden>more ↓</div>}
  </div>;
}

export function ExplanationView({
  explanation: e, story, subject, source, level, language, secondsWaited = 0, onHarder,
}: {
  explanation: Explanation; story?: Story; subject?: string | undefined; source: Src; level: Level; language: Language; secondsWaited?: number; onHarder: (l: Level) => void;
}) {
  const [index, setIndex] = useState(0);
  const [feedback, setFeedback] = useState<string | null>(null);
  const quiz = e.quiz ?? [];
  const [answers, setAnswers] = useState<(number | null)[]>(() => quiz.map(() => null));
  const [qi, setQi] = useState(0);
  const [showScore, setShowScore] = useState(false);
  const scroller = useRef<HTMLDivElement>(null);
  const tracked = useRef("");
  const quizTracked = useRef(false);
  const harder = nextLevel(level);
  const score = answers.filter((a, i) => a !== null && a === quiz[i]?.answer_index).length;
  const topic = story?.topic ?? subject;
  const motifs = useMemo(() => storyMotifs(motifsOf(e), topic), [e, topic]);

  useEffect(() => {
    const k = `${e.headline}|${level}|${language}`;
    if (tracked.current === k) return;
    tracked.current = k;
    trackExplanationShown({ source, level, language, safety: e.safety, seconds_waited: Math.round(secondsWaited) });
  }, [e, level, language, source, secondsWaited]);

  const choose = (q: number, idx: number) => {
    if (answers[q] !== null || !quiz[q]) return;
    const next = answers.map((a, i) => (i === q ? idx : a));
    setAnswers(next);
    trackQuizAnswered({ question_number: q + 1, correct: idx === quiz[q].answer_index, level });
    if (next.every((a) => a !== null)) {
      trackQuizCompleted({ score: next.filter((a, i) => a === quiz[i]?.answer_index).length, total: quiz.length, level });
    }
  };

  // Confetti on the score screen
  useEffect(() => {
    if (!showScore || !quiz.length || reducedMotion()) return;
    const full = score === quiz.length;
    if (!full && score / quiz.length < 0.6) return;
    void import("canvas-confetti").then(({ default: confetti }) => {
      confetti(full ? { particleCount: 160, spread: 100, origin: { y: 0.6 } } : { particleCount: 50, spread: 60, origin: { y: 0.7 } });
    });
  }, [showScore, score, quiz.length]);

  const slides: Slide[] = useMemo(() => {
    const s: Slide[] = [];

    // 1. The gist
    s.push({
      key: "gist", label: "The gist", tone: 1, icon: <Sparkles className="h-4 w-4" />,
      node: (<>
        <div className="flex items-start gap-3">
          <SubjectTile name={topic} size={64} />
          <div className="flex flex-wrap gap-2 pt-1">
            {story ? <><TopicTag topic={story.topic} /><CategoryBadge bucket={story.bucket} /></> : subject ? <TopicTag topic={subject} /> : null}
          </div>
        </div>
        <h1 className="gist-title">{e.headline}</h1>
        {story && (
          <p className="gist-source text-foreground/75">
            From {story.source.name}
            {story.source.url && story.source.url !== "#" && (
              <> · <a href={story.source.url} target="_blank" rel="noreferrer" className="inline-flex items-center gap-1 font-semibold text-primary">Read the original <ExternalLink className="h-3.5 w-3.5" aria-hidden /></a></>
            )}
          </p>
        )}
        {e.safety === "heavy" && e.gentle_note && (
          <div className="gentle flex items-center gap-3 rounded-xl bg-card p-3">
            <Tincu pose="comfort" size={56} /><p>{e.gentle_note}</p>
          </div>
        )}
        {e.in_3_lines?.length > 0 && (
          <section className="gist-lines">
            <h3>In 3 lines</h3>
            <ol>
              {e.in_3_lines.map((l, i) => (
                <li key={i} className="flex gap-3">
                  <span className="num grid shrink-0 place-items-center rounded-full bg-primary font-semibold text-primary-foreground">{i + 1}</span>
                  <span>{l}</span>
                </li>
              ))}
            </ol>
          </section>
        )}
      </>),
    });

    // 2. But why?
    if (e.why_explainers?.length) s.push({
      key: "why", label: "But why?", tone: 2, icon: <HelpCircle className="h-4 w-4" />,
      node: (
        <div className="stack">
          {e.why_explainers.map((w, i) => (
            <section key={i} className="card rounded-xl bg-card">
              <h3>{w.question}</h3>
              <p>{w.answer}</p><SourceBadge source={w.source} />
            </section>
          ))}
        </div>
      ),
    });

    // 3. How we got here
    if (e.backstory?.length) s.push({
      key: "back", label: "How we got here", tone: 3, icon: <History className="h-4 w-4" />,
      node: (
        <ol className="timeline">
          {e.backstory.map((b, i) => (
            <li key={i} className="timeline-item">
              <span className="timeline-dot" aria-hidden />
              <p className="timeline-year">{b.year}</p>
              <p>{b.point}</p><SourceBadge source={b.source} />
            </li>
          ))}
        </ol>
      ),
    });

    // 4. Key people & terms
    if (e.key_people?.length) s.push({
      key: "people", label: "Key people & terms", tone: 3, icon: <Users className="h-4 w-4" />,
      node: (
        <ul className="stack">
          {e.key_people.map((p, i) => (
            <li key={i} className="card person flex gap-3 rounded-xl bg-card">
              <span className="person-icon grid shrink-0 place-items-center rounded-full bg-s3-head text-foreground">
                {p.kind === "term" ? <BookOpen className="h-[1.1em] w-[1.1em]" aria-label="Term" /> : p.kind === "organisation" ? <Building2 className="h-[1.1em] w-[1.1em]" aria-label="Organisation" /> : <User className="h-[1.1em] w-[1.1em]" aria-label="Person" />}
              </span>
              <div className="min-w-0">
                <p className="person-name">{p.name}</p>
                <p className="person-role text-foreground/70">{p.role}{p.country?.trim() ? ` · ${p.country}` : ""}</p>
                <p className="person-why">{p.why_they_matter}</p><SourceBadge source={p.source} />
              </div>
            </li>
          ))}
        </ul>
      ),
    });

    // 5. Connected ideas
    if (e.side_stories?.length) s.push({
      key: "ideas", label: "Connected ideas", tone: 4, icon: <Network className="h-4 w-4" />,
      node: (
        <ul className="stack">
          {e.side_stories.map((x, i) => (
            <li key={i} className="card flex gap-3 rounded-xl bg-card">
              <span className="dot shrink-0 rounded-full bg-teal" aria-hidden />
              <div><p>{x.point}</p><SourceBadge source={x.source} /></div>
            </li>
          ))}
        </ul>
      ),
    });

    // 6. Did you know?
    if (e.fun_facts?.length) s.push({
      key: "facts", label: "Did you know?", tone: 6, icon: <Lightbulb className="h-4 w-4" />,
      node: (
        <ul className="stack">
          {e.fun_facts.map((f, i) => (
            <li key={i} className="card fact flex gap-3 rounded-xl bg-card">
              <Lightbulb className="fact-icon shrink-0 text-sun-deep" aria-hidden />
              <div><p className="font-semibold">{f.point}</p><SourceBadge source={f.source} /></div>
            </li>
          ))}
        </ul>
      ),
    });

    // Feedback and the way into the quiz go at the end of the last reading page
    const last = s[s.length - 1];
    last.node = (<>
      {last.node}
      <div className="feedback-panel rounded-xl bg-card text-center">
        <h3>Did this help you understand?</h3>
        <div className="flex justify-center gap-3">
          {([["happy", "😀", "Yes, it helped"], ["neutral", "😐", "A little"], ["confused", "😕", "Still confused"]] as const).map(([r, face, label]) => (
            <Btn variant="ghost" key={r} aria-label={label} aria-pressed={feedback === r}
              onClick={() => { setFeedback(r); trackFeedbackGiven({ rating: r, source, level }); }}
              className={cn("face grid place-items-center rounded-2xl border-2 transition-colors", feedback === r ? "border-primary bg-primary-soft" : "border-border hover:bg-muted")}>
              {face}
            </Btn>
          ))}
        </div>
        <p className="thanks text-foreground/70" aria-live="polite">{feedback && "Thanks for telling us!"}</p>
        {quiz.length > 0 && <Btn className="check w-full" data-check>Check yourself →</Btn>}
      </div>
    </>);

    // 7. Check yourself
    const q = quiz[qi];
    if (q) {
      const picked = answers[qi] ?? null;
      const lastQ = qi === quiz.length - 1;
      const cheer = score / quiz.length >= 0.6;
      s.push({
        key: "quiz", label: "Check yourself", tone: 5, icon: <Trophy className="h-4 w-4" />, quiz: true,
        node: showScore ? (
          <div className="quiz-score flex flex-col items-center text-center">
            <Tincu pose={cheer ? "cheer" : "comfort"} size={96} />
            <h2>{score} out of {quiz.length}</h2>
            <p className="score-msg">{message(score, quiz.length)}</p>
            <p className="score-note text-foreground/70">Your score is just for you. No one else sees it.</p>
            <div className="quiz-actions flex w-full flex-col">
              <Btn size="lg" data-reset>Read again</Btn>
              <Link to="/" className={btn({ variant: "soft", size: "lg" })}>Explain another</Link>
              {harder && <Btn variant="soft" size="lg" onClick={() => onHarder(harder)}>Try a harder class ({harder})</Btn>}
            </div>
          </div>
        ) : (<>
          <div className="quiz-progress flex items-center gap-2" aria-label={`Question ${qi + 1} of ${quiz.length}`}>
            <span className="font-bold">Question {qi + 1} of {quiz.length}</span>
            <span className="flex flex-1 gap-1" aria-hidden>
              {quiz.map((_, i) => <span key={i} className={cn("h-1.5 flex-1 rounded-full", i <= qi ? "bg-sun-deep" : "bg-card")} />)}
            </span>
          </div>
          <h2 className="quiz-question">{q.question}</h2>
          <div className="quiz-options grid grid-cols-2">
            {q.options.map((opt, idx) => {
              const isRight = picked !== null && idx === q.answer_index;
              const isWrong = picked === idx && idx !== q.answer_index;
              return (
                <Btn variant="ghost" key={idx} onClick={() => choose(qi, idx)} disabled={picked !== null}
                  className={cn("rounded-xl border-2 text-left font-medium transition-colors",
                    isRight ? "border-success bg-success-soft text-success" : isWrong ? "border-coral bg-s3 text-foreground" : "border-sun-deep bg-card hover:bg-s5",
                    picked !== null && !isRight && !isWrong && "opacity-50")}>
                  <span>{opt}</span>
                  {isRight && <span className="verdict flex items-center gap-1 font-semibold"><Check className="h-[1.2em] w-[1.2em]" aria-hidden />Right</span>}
                  {isWrong && <span className="verdict flex items-center gap-1 font-semibold"><X className="h-[1.2em] w-[1.2em]" aria-hidden />Not quite</span>}
                </Btn>
              );
            })}
          </div>
          <div className="quiz-reason" aria-live="polite">
            {picked !== null && <p className="rounded-xl bg-card"><strong>Why: </strong>{q.why}</p>}
          </div>
          {picked !== null && (
            <Btn size="lg" className="quiz-next w-full" onClick={() => (lastQ ? setShowScore(true) : setQi(qi + 1))}>
              {lastQ ? "See my score" : "Next question"}
            </Btn>
          )}
        </>),
      });
    }
    return s;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [e, story, subject, topic, feedback, answers, qi, showScore, score, harder, level, source]);

  const n = slides.length;
  const quizIndex = slides.findIndex((s) => s.quiz);

  const goTo = useCallback((i: number) => {
    const el = scroller.current;
    if (!el) return;
    el.scrollTo({ left: i * el.clientWidth, behavior: reducedMotion() ? "auto" : "smooth" });
  }, []);

  useEffect(() => {
    if (quizIndex >= 0 && index === quizIndex && !quizTracked.current) {
      quizTracked.current = true;
      trackQuizStarted({ source, level });
    }
  }, [index, quizIndex, source, level]);

  useEffect(() => {
    const onKey = (ev: KeyboardEvent) => {
      const t = ev.target as HTMLElement | null;
      if (t && (t.closest("input,textarea,select") || t.isContentEditable)) return;
      if (ev.key === "ArrowRight") { ev.preventDefault(); goTo(Math.min(index + 1, n - 1)); }
      if (ev.key === "ArrowLeft") { ev.preventDefault(); goTo(Math.max(index - 1, 0)); }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [index, n, goTo]);

  const onScroll = () => {
    const el = scroller.current;
    if (!el) return;
    const i = Math.round(el.scrollLeft / el.clientWidth);
    if (i !== index) setIndex(i);
  };

  const onSlideClick = (ev: MouseEvent<HTMLDivElement>) => {
    const t = ev.target as HTMLElement;
    if (t.closest("[data-check]")) { goTo(quizIndex); return; }
    if (t.closest("[data-reset]")) { setAnswers(quiz.map(() => null)); setQi(0); setShowScore(false); goTo(0); return; }
    if (t.closest("button,a,select,input,summary,details,[role=button]")) return;
    const r = ev.currentTarget.getBoundingClientRect();
    const x = (ev.clientX - r.left) / r.width;
    const edge = r.width > 600 ? 0.15 : 0.3;
    if (x > 1 - edge) goTo(Math.min(index + 1, n - 1));
    else if (x < edge) goTo(Math.max(index - 1, 0));
  };

  return (
    <div className="explanation-deck flex h-full min-h-0 flex-col overflow-hidden">
      <div className="mx-auto flex w-full max-w-3xl gap-1 px-4 pt-3" role="progressbar" aria-label="Card progress" aria-valuemin={1} aria-valuemax={n} aria-valuenow={index + 1}>
        {slides.map((s, i) => (
          <span key={s.key} className={cn("h-1.5 flex-1 rounded-full transition-colors", i <= index ? (s.quiz ? "bg-sun" : "bg-primary") : "bg-border")} />
        ))}
      </div>

      <div ref={scroller} onScroll={onScroll}
        className="no-scrollbar flex min-h-0 flex-1 snap-x snap-mandatory overflow-x-auto overscroll-x-contain"
        aria-roledescription="carousel" aria-label="Explanation cards">
        {slides.map((s, i) => {
          const t = TONE[s.tone];
          return (
            <div key={s.key} className="slide-page h-full w-full shrink-0 snap-center snap-always" inert={i !== index}>
              <div onClick={onSlideClick} role="group" aria-roledescription="slide" aria-label={`${i + 1} of ${n}: ${s.label}`}
                className={cn("slide-card mx-auto flex h-full flex-col overflow-hidden rounded-2xl border border-border", t.bg)}>
                <div className={cn("slide-header flex shrink-0 items-center", t.head)}>
                  <span className={cn("grid place-items-center rounded-full", t.ink)} aria-hidden>{s.icon}</span>
                  <h2 className="font-bold">{s.label}</h2>
                  <span className="ml-auto font-semibold text-foreground/70">{i + 1} / {n}</span>
                </div>
                <SlideBody tone={s.tone} motifs={motifs} seed={i}>{s.node}</SlideBody>
              </div>
            </div>
          );
        })}
      </div>

      <div className="mx-auto flex w-full max-w-xl items-center justify-between px-4 pb-4">
        <Btn variant="ghost" onClick={() => goTo(index - 1)} disabled={index === 0} aria-label="Previous card"
          className="grid h-11 w-11 place-items-center rounded-full border border-border bg-card disabled:opacity-30">
          <ChevronLeft className="h-5 w-5" aria-hidden />
        </Btn>
        <span className="text-sm font-medium text-foreground/70" aria-live="polite">{index + 1} / {n}</span>
        <Btn variant="ghost" onClick={() => goTo(index + 1)} disabled={index >= n - 1} aria-label="Next card"
          className="grid h-11 w-11 place-items-center rounded-full bg-primary text-primary-foreground disabled:opacity-30">
          <ChevronRight className="h-5 w-5" aria-hidden />
        </Btn>
      </div>
    </div>
  );
}
