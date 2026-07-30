<template>
  <div class="quiz-container">
    <div class="quiz-header">
      <span class="quiz-icon">🧩</span>
      <h3>本章 Quiz — 核心机制自检</h3>
    </div>
    <div class="quiz-body">
      <div
        v-for="(q, qi) in questions"
        :key="qi"
        class="quiz-question"
      >
        <p class="quiz-question-text">
          <span class="quiz-num">{{ qi + 1 }}</span>
          {{ q.question }}
        </p>
        <div class="quiz-options">
          <button
            v-for="(opt, oi) in q.options"
            :key="oi"
            :class="optionClass(qi, oi)"
            :disabled="answered[qi]"
            @click="select(qi, oi)"
          >
            <span class="quiz-opt-label">{{ label(oi) }}</span>
            <span class="quiz-opt-text">{{ opt }}</span>
          </button>
        </div>
        <Transition name="quiz-fade">
          <div v-if="answered[qi]" class="quiz-explanation">
            <div class="quiz-result-badge" :class="selected[qi] === q.correct ? 'correct' : 'wrong'">
              {{ selected[qi] === q.correct ? '✅ 回答正确' : '❌ 回答错误' }}
            </div>
            <p class="quiz-explanation-text">{{ q.explanation }}</p>
          </div>
        </Transition>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, watch } from 'vue'

interface Question {
  question: string
  options: string[]
  correct: number
  explanation: string
}

const props = defineProps<{
  questions?: Question[]
}>()

const safeQuestions = computed(() => props.questions ?? [])
const answered = ref<boolean[]>(new Array(safeQuestions.value.length).fill(false))
const selected = ref<number[]>(new Array(safeQuestions.value.length).fill(-1))

watch(() => safeQuestions.value.length, (len) => {
  answered.value = new Array(len).fill(false)
  selected.value = new Array(len).fill(-1)
}, { immediate: true })

function label(index: number): string {
  return String.fromCharCode(65 + index)
}

function select(qIdx: number, oIdx: number): void {
  if (answered.value[qIdx]) return
  selected.value[qIdx] = oIdx
  answered.value[qIdx] = true
}

function optionClass(qIdx: number, oIdx: number): string {
  const base = 'quiz-option'
  if (!answered.value[qIdx]) return base
  const sel = selected.value[qIdx]
  const corr = props.questions[qIdx].correct
  if (oIdx === corr) return `${base} correct`
  if (oIdx === sel && oIdx !== corr) return `${base} wrong`
  return `${base} disabled`
}
</script>

<style scoped>
.quiz-container {
  margin: 3rem 0 2rem;
  border: 1px solid var(--vp-c-border);
  border-radius: 12px;
  background: var(--vp-c-bg-soft);
  overflow: hidden;
}

.quiz-header {
  display: flex;
  align-items: center;
  gap: 0.6rem;
  padding: 1rem 1.25rem;
  background: var(--vp-c-bg-alt);
  border-bottom: 1px solid var(--vp-c-border);
}

.quiz-icon {
  font-size: 1.3rem;
  line-height: 1;
}

.quiz-header h3 {
  margin: 0;
  font-size: 1.05rem;
  font-weight: 600;
  color: var(--vp-c-text-1);
}

.quiz-body {
  padding: 1.25rem;
}

.quiz-question {
  margin-bottom: 1.5rem;
}

.quiz-question:last-child {
  margin-bottom: 0;
}

.quiz-question-text {
  margin: 0 0 0.75rem;
  font-size: 0.95rem;
  font-weight: 500;
  line-height: 1.6;
  color: var(--vp-c-text-1);
}

.quiz-num {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 1.5rem;
  height: 1.5rem;
  margin-right: 0.5rem;
  border-radius: 50%;
  background: var(--vp-c-brand-1);
  color: #fff;
  font-size: 0.75rem;
  font-weight: 700;
  flex-shrink: 0;
}

.quiz-options {
  display: flex;
  flex-direction: column;
  gap: 0.5rem;
}

.quiz-option {
  display: flex;
  align-items: center;
  gap: 0.6rem;
  padding: 0.6rem 0.9rem;
  border: 1px solid var(--vp-c-border);
  border-radius: 8px;
  background: var(--vp-c-bg);
  color: var(--vp-c-text-1);
  font-size: 0.9rem;
  text-align: left;
  cursor: pointer;
  transition: all 0.15s ease;
}

.quiz-option:hover:not(:disabled) {
  border-color: var(--vp-c-brand-1);
  background: var(--vp-c-bg-soft);
}

.quiz-option.correct {
  border-color: #34d399;
  background: rgba(52, 211, 153, 0.08);
}

.quiz-option.wrong {
  border-color: #f87171;
  background: rgba(248, 113, 113, 0.08);
}

.quiz-option.disabled {
  opacity: 0.6;
  cursor: default;
}

.quiz-opt-label {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 1.4rem;
  height: 1.4rem;
  border-radius: 4px;
  border: 1px solid var(--vp-c-border);
  font-size: 0.75rem;
  font-weight: 600;
  color: var(--vp-c-text-2);
  flex-shrink: 0;
}

.quiz-option.correct .quiz-opt-label {
  background: #34d399;
  border-color: #34d399;
  color: #fff;
}

.quiz-option.wrong .quiz-opt-label {
  background: #f87171;
  border-color: #f87171;
  color: #fff;
}

.quiz-explanation {
  margin-top: 0.75rem;
  padding: 0.75rem 1rem;
  border-radius: 8px;
  background: var(--vp-c-bg);
  border: 1px solid var(--vp-c-border);
}

.quiz-result-badge {
  display: inline-block;
  margin-bottom: 0.5rem;
  padding: 0.25rem 0.6rem;
  border-radius: 4px;
  font-size: 0.8rem;
  font-weight: 600;
}

.quiz-result-badge.correct {
  background: rgba(52, 211, 153, 0.12);
  color: #34d399;
}

.quiz-result-badge.wrong {
  background: rgba(248, 113, 113, 0.12);
  color: #f87171;
}

.quiz-explanation-text {
  margin: 0;
  font-size: 0.88rem;
  line-height: 1.6;
  color: var(--vp-c-text-2);
}

.quiz-fade-enter-active,
.quiz-fade-leave-active {
  transition: opacity 0.25s ease;
}

.quiz-fade-enter-from,
.quiz-fade-leave-to {
  opacity: 0;
}
</style>
