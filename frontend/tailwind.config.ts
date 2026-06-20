import type { Config } from 'tailwindcss';

const config: Config = {
  content: [
    './src/app/**/*.{js,ts,jsx,tsx,mdx}',
    './src/components/**/*.{js,ts,jsx,tsx,mdx}',
    './src/lib/**/*.{js,ts,jsx,tsx,mdx}',
  ],
  theme: {
    extend: {
      colors: {
        // ── Linear 设计系统：表面色 ────────────────────────────────────────
        bg: '#0A0B0E',
        surface: {
          0: '#111216',
          1: '#1C1D21',
          2: '#232429',
          3: '#2C2D31',
        },
        // ── 品牌色 ────────────────────────────────────────────────────────
        brand: {
          DEFAULT: '#5E6AD2',
          light: '#8B7CF6',
          bg: 'rgba(94,106,210,.08)',
          border: 'rgba(94,106,210,.15)',
        },
        // ── 文字层级 ────────────────────────────────────────────────────────
        t: {
          1: '#F4F4F5',
          2: '#A0A3AB',
          3: '#6B6F76',
          4: '#4A4D54',
        },
        // ── 交互态 ────────────────────────────────────────────────────────
        hover: 'rgba(255,255,255,.04)',
        active: 'rgba(255,255,255,.06)',
        // ── 边框 ────────────────────────────────────────────────────────────
        border: {
          DEFAULT: 'rgba(255,255,255,.06)',
          hover: 'rgba(255,255,255,.12)',
          subtle: 'rgba(255,255,255,.08)',
        },
        // ── 状态色 ──────────────────────────────────────────────────────────
        status: {
          green: '#4CB782',
          'green-bg': 'rgba(76,183,130,.12)',
          yellow: '#E8B84C',
          'yellow-bg': 'rgba(232,184,76,.12)',
          red: '#E85D5D',
          'red-bg': 'rgba(232,93,93,.12)',
          gray: '#6B6F76',
          'gray-bg': 'rgba(107,111,118,.12)',
        },
        // ── 环境徽章配色（保留） ────────────────────────────────────────────
        env: {
          demo: '#6366F1',
          simulation: '#0EA5E9',
          'real-read': '#10B981',
          'real-trade': '#F97316',
        },
      },
      borderRadius: {
        xs: '6px',
        sm: '8px',
        lg: '12px',
      },
      fontSize: {
        '2xs': '11px',
        'xs': '12px',
        sm: '13px',
        base: '14px',
      },
      maxWidth: {
        content: '1120px',
      },
      animation: {
        'fade-up': 'fadeUp .4s ease both',
        pulse: 'pulse 2s ease-in-out infinite',
      },
      keyframes: {
        fadeUp: {
          from: { opacity: '0', transform: 'translateY(8px)' },
          to: { opacity: '1', transform: 'translateY(0)' },
        },
        pulse: {
          '0%, 100%': { opacity: '1' },
          '50%': { opacity: '.4' },
        },
      },
    },
  },
  plugins: [],
};

export default config;
