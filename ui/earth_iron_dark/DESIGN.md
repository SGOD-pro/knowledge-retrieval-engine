---
name: Earth & Iron Dark
colors:
  surface: '#121416'
  surface-dim: '#121416'
  surface-bright: '#37393b'
  surface-container-lowest: '#0c0e10'
  surface-container-low: '#1a1c1e'
  surface-container: '#1e2022'
  surface-container-high: '#282a2c'
  surface-container-highest: '#333537'
  on-surface: '#e2e2e5'
  on-surface-variant: '#dcc1b8'
  inverse-surface: '#e2e2e5'
  inverse-on-surface: '#2f3133'
  outline: '#a48b84'
  outline-variant: '#56423c'
  surface-tint: '#ffb59d'
  primary: '#ffb59d'
  on-primary: '#5d1800'
  primary-container: '#dc7350'
  on-primary-container: '#521400'
  inverse-primary: '#9d4324'
  secondary: '#c6c6c9'
  on-secondary: '#2f3133'
  secondary-container: '#454749'
  on-secondary-container: '#b4b5b7'
  tertiary: '#6cd7d8'
  on-tertiary: '#003737'
  tertiary-container: '#29a0a1'
  on-tertiary-container: '#002f30'
  error: '#ffb4ab'
  on-error: '#690005'
  error-container: '#93000a'
  on-error-container: '#ffdad6'
  primary-fixed: '#ffdbd0'
  primary-fixed-dim: '#ffb59d'
  on-primary-fixed: '#390b00'
  on-primary-fixed-variant: '#7e2c0e'
  secondary-fixed: '#e2e2e5'
  secondary-fixed-dim: '#c6c6c9'
  on-secondary-fixed: '#1a1c1e'
  on-secondary-fixed-variant: '#454749'
  tertiary-fixed: '#89f3f4'
  tertiary-fixed-dim: '#6cd7d8'
  on-tertiary-fixed: '#002020'
  on-tertiary-fixed-variant: '#004f50'
  background: '#121416'
  on-background: '#e2e2e5'
  surface-variant: '#333537'
  text-primary: '#E2E2E2'
  text-muted: '#C4C7C5'
  iron-deep: '#1A1C1E'
  iron-mid: '#2D2F31'
  iron-light: '#3E4143'
  terracotta-accent: '#C96442'
typography:
  headline-xl:
    fontFamily: Literata
    fontSize: 48px
    fontWeight: '700'
    lineHeight: '1.1'
    letterSpacing: -0.02em
  headline-lg:
    fontFamily: Literata
    fontSize: 32px
    fontWeight: '600'
    lineHeight: '1.2'
    letterSpacing: -0.01em
  headline-lg-mobile:
    fontFamily: Literata
    fontSize: 28px
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
  xs: 4px
  sm: 8px
  md: 16px
  lg: 24px
  xl: 40px
  gutter: 24px
  margin-mobile: 16px
  margin-desktop: 64px
---

## Brand & Style

This design system is a sophisticated, dark-mode evolution of the "Earth & Iron" aesthetic, tailored for professional retrieval tasks and deep work. It blends **Minimalism** with **Modern Corporate** sensibilities, emphasizing high legibility and reduced eye strain. 

The personality is authoritative yet tactile—evoking the feeling of a modern architectural studio or a high-end editorial desk at night. By pairing the organic warmth of Terracotta with the cold stability of Iron Grey, the UI achieves a balanced, grounded atmosphere. The focus is on content hierarchy, utilizing generous whitespace and precise typography to guide the user through complex information environments.

## Colors

The palette is anchored by "Iron Deep" (#1A1C1E) for primary surfaces, providing a low-glare foundation for professional environments. "Iron Mid" (#2D2F31) acts as the secondary surface for cards and containers, creating subtle depth.

The brand's signature **Terracotta** (#C96442) is retained as the primary accent. In this dark context, it serves as a high-contrast beacon for calls to action and critical status indicators. Text roles are strictly bifurcated: Off-white (#E2E2E2) ensures crisp, authoritative headers, while Muted Grey (#C4C7C5) reduces the cognitive load for long-form body content. All interactive states should prioritize maintaining a 4.5:1 contrast ratio against the charcoal base.

## Typography

The typography maintains an editorial soul by using **Literata** for all headlines. Its classic serif structure provides an intellectual contrast to the technical dark theme. For data-heavy tasks and body text, **Work Sans** provides a neutral, highly legible grotesque foundation that thrives in digital interfaces.

- **Headlines:** Use Literata with tighter letter-spacing for large displays to emphasize the "editorial" look.
- **Body:** Use Work Sans with a generous line-height (1.5 - 1.6) to prevent text from feeling "crowded" against the dark background.
- **Micro-copy:** Use `label-sm` in all-caps with increased letter-spacing for metadata and sidebar category headers.

## Layout & Spacing

This design system utilizes a **12-column fluid grid** for desktop and a **4-column grid** for mobile. The layout philosophy is centered on "Information Clusters"—grouping related data into distinct containers to manage complexity.

- **Desktop:** 64px outer margins with 24px gutters. Use the `xl` (40px) spacing unit to separate major content sections.
- **Mobile:** 16px outer margins. Elements should reflow to full-width containers, with vertical spacing reduced to the `md` (16px) unit.
- **Rhythm:** A 4px base unit governs all padding and margins to ensure mathematical harmony across the interface.

## Elevation & Depth

In this dark mode variation, hierarchy is established through **Tonal Layering** rather than traditional shadows. 

- **Level 0 (Base):** Iron Deep (#1A1C1E) for the main application background.
- **Level 1 (Containers):** Iron Mid (#2D2F31) for cards, sidebars, and navigation rails.
- **Level 2 (Interaction):** Iron Light (#3E4143) for hovered states or secondary buttons.

To maintain the sophisticated aesthetic, use **Low-contrast outlines** (1px solid Iron Light at 50% opacity) for cards instead of heavy shadows. This keeps the interface feeling flat, architectural, and modern. Small, high-diffusion shadows (0.1 opacity black) may be used sparingly on floating modals to differentiate them from the primary surface.

## Shapes

The system adopts a **Rounded** (Level 2) geometry, specifically utilizing a `rounded-2xl` (1.5rem / 24px) standard for primary cards and main containers. This generous radius softens the "Iron" color palette, making the professional environment feel more approachable and modern.

- **Primary Buttons:** Fully pill-shaped to stand out against the geometric grid.
- **Input Fields & Small Components:** Use `rounded-md` (0.75rem) to maintain internal density.
- **Main Content Containers:** Use `rounded-xl` (1.5rem) to define clear, distinct regions of information.

## Components

### Buttons
- **Primary:** Filled Terracotta (#C96442) with white text. High-contrast, rounded-full.
- **Secondary:** Ghost style with an Iron Light border and off-white text.
- **Tertiary:** Text-only in Muted Grey, shifting to Off-white on hover.

### Cards
Cards are the primary structural unit. Use Iron Mid (#2D2F31) with a 1.5rem corner radius. Padding should follow the `lg` (24px) spacing unit.

### Inputs
Input fields should use a slightly darker or matching surface to the container they sit in, defined by a 1px Iron Light border. Focus states use a 2px Terracotta ring.

### Chips & Tags
Small, `rounded-md` elements using Iron Light background with Muted Grey text. These should be subtle and never compete with primary buttons.

### Lists & Navigation
Use subtle horizontal dividers in Iron Light (20% opacity). Active navigation items are indicated by a vertical Terracotta bar (4px width) on the leading edge or a subtle Terracotta text tint.