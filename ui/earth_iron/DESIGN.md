---
name: Earth & Iron Light
colors:
  surface: '#fff8f6'
  surface-dim: '#e9d6d1'
  surface-bright: '#fff8f6'
  surface-container-lowest: '#ffffff'
  surface-container-low: '#fff1ed'
  surface-container: '#fdeae4'
  surface-container-high: '#f7e4df'
  surface-container-highest: '#f1dfd9'
  on-surface: '#231916'
  on-surface-variant: '#56423c'
  inverse-surface: '#392e2b'
  inverse-on-surface: '#ffede8'
  outline: '#89726b'
  outline-variant: '#dcc1b8'
  surface-tint: '#9d4324'
  primary: '#9a4021'
  on-primary: '#ffffff'
  primary-container: '#b95837'
  on-primary-container: '#fffbff'
  inverse-primary: '#ffb59d'
  secondary: '#605f57'
  on-secondary: '#ffffff'
  secondary-container: '#e5e2d8'
  on-secondary-container: '#66655d'
  tertiary: '#006768'
  on-tertiary: '#ffffff'
  tertiary-container: '#008283'
  on-tertiary-container: '#f3fffe'
  error: '#ba1a1a'
  on-error: '#ffffff'
  error-container: '#ffdad6'
  on-error-container: '#93000a'
  primary-fixed: '#ffdbd0'
  primary-fixed-dim: '#ffb59d'
  on-primary-fixed: '#390b00'
  on-primary-fixed-variant: '#7e2c0e'
  secondary-fixed: '#e5e2d8'
  secondary-fixed-dim: '#c9c6bd'
  on-secondary-fixed: '#1c1c16'
  on-secondary-fixed-variant: '#484740'
  tertiary-fixed: '#89f4f4'
  tertiary-fixed-dim: '#6cd7d8'
  on-tertiary-fixed: '#002020'
  on-tertiary-fixed-variant: '#004f50'
  background: '#fff8f6'
  on-background: '#231916'
  surface-variant: '#f1dfd9'
  terracotta-primary: '#c96442'
  clay-background: '#faf9f5'
  iron-foreground: '#3d3929'
  sand-sidebar: '#f5f4ee'
  chart-rust: '#b05730'
  chart-lavender: '#9c87f5'
  chart-ecru: '#ded8c4'
typography:
  headline-xl:
    fontFamily: Literata
    fontSize: 48px
    fontWeight: '700'
    lineHeight: '1.1'
  headline-lg:
    fontFamily: Literata
    fontSize: 32px
    fontWeight: '600'
    lineHeight: '1.2'
  headline-md:
    fontFamily: Literata
    fontSize: 24px
    fontWeight: '600'
    lineHeight: '1.3'
  body-lg:
    fontFamily: Work Sans
    fontSize: 18px
    fontWeight: '400'
    lineHeight: '1.6'
  body-md:
    fontFamily: Work Sans
    fontSize: 16px
    fontWeight: '400'
    lineHeight: '1.5'
  label-md:
    fontFamily: Work Sans
    fontSize: 14px
    fontWeight: '500'
    lineHeight: '1.4'
    letterSpacing: 0.02em
  label-sm:
    fontFamily: Work Sans
    fontSize: 12px
    fontWeight: '600'
    lineHeight: '1.2'
    letterSpacing: 0.05em
rounded:
  sm: 0.25rem
  DEFAULT: 0.5rem
  md: 0.75rem
  lg: 1rem
  xl: 1.5rem
  full: 9999px
spacing:
  base: 4px
  xs: 4px
  sm: 8px
  md: 16px
  lg: 24px
  xl: 40px
  gutter: 24px
  margin-mobile: 16px
  margin-desktop: 64px
---

:root {
  --card: #faf9f5;
  --ring: #c96442;
  --input: #b4b2a7;
  --muted: #ede9de;
  --accent: #e9e6dc;
  --border: #dad9d4;
  --radius: 0.5rem;
  --chart-1: #b05730;
  --chart-2: #9c87f5;
  --chart-3: #ded8c4;
  --chart-4: #dbd3f0;
  --chart-5: #b4552d;
  --popover: #ffffff;
  --primary: #c96442;
  --sidebar: #f5f4ee;
  --secondary: #e9e6dc;
  --background: #faf9f5;
  --foreground: #3d3929;
  --destructive: #141413;
  --sidebar-ring: #b5b5b5;
  --sidebar-accent: #e9e6dc;
  --sidebar-border: #ebebeb;
  --card-foreground: #141413;
  --sidebar-primary: #c96442;
  --muted-foreground: #83827d;
  --accent-foreground: #28261b;
  --popover-foreground: #28261b;
  --primary-foreground: #ffffff;
  --sidebar-foreground: #3d3d3a;
  --secondary-foreground: #535146;
  --destructive-foreground: #ffffff;
  --sidebar-accent-foreground: #343434;
  --sidebar-primary-foreground: #fbfbfb;
}

.dark {
  --card: #262624;
  --ring: #d97757;
  --input: #52514a;
  --muted: #1b1b19;
  --accent: #1a1915;
  --border: #3e3e38;
  --chart-1: #b05730;
  --chart-2: #9c87f5;
  --chart-3: #1a1915;
  --chart-4: #2f2b48;
  --chart-5: #b4552d;
  --popover: #30302e;
  --primary: #d97757;
  --sidebar: #1f1e1d;
  --secondary: #faf9f5;
  --background: #262624;
  --foreground: #c3c0b6;
  --destructive: #ef4444;
  --sidebar-ring: #b5b5b5;
  --sidebar-accent: #0f0f0e;
  --sidebar-border: #ebebeb;
  --card-foreground: #faf9f5;
  --sidebar-primary: #343434;
  --muted-foreground: #b7b5a9;
  --accent-foreground: #f5f4ee;
  --popover-foreground: #e5e5e2;
  --primary-foreground: #ffffff;
  --sidebar-foreground: #c3c0b6;
  --secondary-foreground: #30302e;
  --destructive-foreground: #ffffff;
  --sidebar-accent-foreground: #c3c0b6;
  --sidebar-primary-foreground: #fbfbfb;
}

@theme inline {
  --color-card: var(--card);
  --color-ring: var(--ring);
  --color-input: var(--input);
  --color-muted: var(--muted);
  --color-accent: var(--accent);
  --color-border: var(--border);
  --color-radius: var(--radius);
  --color-chart-1: var(--chart-1);
  --color-chart-2: var(--chart-2);
  --color-chart-3: var(--chart-3);
  --color-chart-4: var(--chart-4);
  --color-chart-5: var(--chart-5);
  --color-popover: var(--popover);
  --color-primary: var(--primary);
  --color-sidebar: var(--sidebar);
  --color-secondary: var(--secondary);
  --color-background: var(--background);
  --color-foreground: var(--foreground);
  --color-destructive: var(--destructive);
  --color-sidebar-ring: var(--sidebar-ring);
  --color-sidebar-accent: var(--sidebar-accent);
  --color-sidebar-border: var(--sidebar-border);
  --color-card-foreground: var(--card-foreground);
  --color-sidebar-primary: var(--sidebar-primary);
  --color-muted-foreground: var(--muted-foreground);
  --color-accent-foreground: var(--accent-foreground);
  --color-popover-foreground: var(--popover-foreground);
  --color-primary-foreground: var(--primary-foreground);
  --color-sidebar-foreground: var(--sidebar-foreground);
  --color-secondary-foreground: var(--secondary-foreground);
  --color-destructive-foreground: var(--destructive-foreground);
  --color-sidebar-accent-foreground: var(--sidebar-accent-foreground);
  --color-sidebar-primary-foreground: var(--sidebar-primary-foreground);
}