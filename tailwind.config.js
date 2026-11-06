/** Content scanning matches the old cdn.tailwindcss.com JIT exactly:
 *  templates (including their inline <script> blocks), Python (classes
 *  generated there, e.g. BMI status_class), and static JS. */
module.exports = {
  content: [
    "./templates/**/*.html",
    "./app/**/*.py",
    "./static/*.js",
  ],
  theme: {
    extend: {
      colors: {
        bg: '#0A0A0A',
        bg2: '#0E0E0E',
        bg3: '#121212',
        yellow: '#fef08a',
        green: '#4ade80',
      },
      fontFamily: {
        sans: ['Inter', 'system-ui', 'sans-serif'],
        mono: ['JetBrains Mono', 'monospace'],
      },
    },
  },
  corePlugins: {
    preflight: true,
  },
}
