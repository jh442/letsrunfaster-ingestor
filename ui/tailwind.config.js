/** @type {import('tailwindcss').Config} */
export default {
  // main.js builds class strings at runtime from literal fragments, so it has
  // to be scanned too or the coloured state classes vanish in the build.
  content: ['./index.html', './src/**/*.{js,css}'],
  theme: {
    extend: {
      fontFamily: {
        sans: [
          '"Plus Jakarta Sans Variable"',
          'ui-sans-serif',
          'system-ui',
          '-apple-system',
          '"Segoe UI"',
          'sans-serif',
        ],
        display: ['"Space Grotesk Variable"', '"Plus Jakarta Sans Variable"', 'sans-serif'],
      },
    },
  },
  plugins: [],
};
