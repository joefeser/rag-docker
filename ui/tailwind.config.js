/** @type {import('tailwindcss').Config} */
import typography from '@tailwindcss/typography'

export default {
  content: ['./index.html', './src/**/*.{js,ts,jsx,tsx}'],
  theme: {
    extend: {},
  },
  // `prose` is used by the Q&A answer pane and the transfer help page. Without
  // this plugin those classes compile to nothing and Tailwind's preflight has
  // already stripped heading and list styling, so markdown renders as one
  // undifferentiated block.
  plugins: [typography],
}
