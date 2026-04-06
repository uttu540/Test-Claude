/** @type {import('tailwindcss').Config} */
export default {
  content: [
    "./index.html",
    "./src/**/*.{js,jsx}",
  ],
  theme: {
    extend: {
      colors: {
        bg: {
          primary: '#0f0f0f',
          card: '#1a1a1a',
          hover: '#222222',
        },
        border: {
          DEFAULT: '#2a2a2a',
          subtle: '#1f1f1f',
        },
        green: {
          trade: '#22c55e',
          dim: '#16a34a',
          muted: 'rgba(34,197,94,0.1)',
        },
        red: {
          trade: '#ef4444',
          dim: '#dc2626',
          muted: 'rgba(239,68,68,0.1)',
        },
        yellow: {
          trade: '#eab308',
          dim: '#ca8a04',
          muted: 'rgba(234,179,8,0.1)',
        },
        blue: {
          trade: '#3b82f6',
          dim: '#2563eb',
          muted: 'rgba(59,130,246,0.1)',
        },
        cyan: {
          trade: '#06b6d4',
          muted: 'rgba(6,182,212,0.1)',
        },
        text: {
          primary: '#f1f5f9',
          secondary: '#94a3b8',
          muted: '#475569',
        },
      },
      fontFamily: {
        sans: ['Inter', 'system-ui', 'sans-serif'],
        mono: ['JetBrains Mono', 'Menlo', 'Monaco', 'monospace'],
      },
      fontSize: {
        '2xs': ['0.65rem', { lineHeight: '1rem' }],
      },
      animation: {
        'pulse-green': 'pulseGreen 2s ease-in-out infinite',
        'fade-in': 'fadeIn 0.3s ease-out',
        'slide-up': 'slideUp 0.3s ease-out',
        'blink': 'blink 1.2s step-end infinite',
      },
      keyframes: {
        pulseGreen: {
          '0%, 100%': { boxShadow: '0 0 0 0 rgba(34,197,94,0.4)' },
          '50%': { boxShadow: '0 0 0 6px rgba(34,197,94,0)' },
        },
        fadeIn: {
          from: { opacity: '0' },
          to: { opacity: '1' },
        },
        slideUp: {
          from: { opacity: '0', transform: 'translateY(8px)' },
          to: { opacity: '1', transform: 'translateY(0)' },
        },
        blink: {
          '0%, 100%': { opacity: '1' },
          '50%': { opacity: '0' },
        },
      },
    },
  },
  plugins: [],
}
