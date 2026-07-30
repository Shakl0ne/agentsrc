import DefaultTheme from 'vitepress/theme'
import './style.css'
import Quiz from './components/Quiz.vue'

export default {
  extends: DefaultTheme,
  enhanceApp({ app }) {
    app.component('Quiz', Quiz)
  }
}
