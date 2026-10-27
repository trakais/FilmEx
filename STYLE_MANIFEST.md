# FilmEx Style Manifest

## Purpose

This document describes the **visual language** of FilmEx so it can be
reapplied to another product without copying the literal CSS. It is not a
component inventory. It is the transfer spec for the theme's tone, tokens,
layout posture, and interaction cues.

The current FilmEx theme is defined in
[filmex/static/style-rangefinder.css](filmex/static/style-rangefinder.css).

## Style Concept

FilmEx uses an **instrument-panel / rangefinder-camera** visual direction.
It should feel:

- machined, not soft
- precise, not decorative
- tactile, not glossy
- dark, but not pitch-black
- technical, but not sci-fi
- premium, but not luxurious

The theme is closer to a **Leica-adjacent control surface** than a gallery,
dashboard, or typical SaaS admin UI.

Core references:

- graphite metal
- vulcanite texture
- engraved labels
- calibration markings
- one decisive red control
- yellow measurement/readout accents

## Core Ideas

1. **Typography does most of the branding.**
   The look is carried by condensed industrial type, uppercase micro-labels,
   and tabular numerals more than by illustrations or large decorative
   shapes.

2. **The UI behaves like a tool, not a website.**
   Sections read like controls, selectors, readouts, and panels. Avoid soft
   cards, marketing gradients, oversized empty space, and lifestyle framing.

3. **Dark surfaces use tonal separation, not black-vs-gray contrast.**
   Several very close near-black values create depth. Pure black is avoided.

4. **Accent colors have strict jobs.**
   Yellow is for calibration, focus, active readouts, and selected-state
   cues. Red is reserved for the single strongest action or destructive
   action. White is for primary legibility, not for emphasis.

5. **Geometry stays sharp and controlled.**
   Corners are effectively square. Radii are minimal. Buttons and tiles feel
   machined, not padded.

6. **Motion and chrome stay restrained.**
   Hover states are subtle. Shadows are almost absent. Surfaces do not float.
   State changes happen through borders, label color, ticks, corner marks,
   and small tonal shifts.

## Color System

### Foundation neutrals

Use these as the base palette. They define almost the entire atmosphere.

| Token | Hex | Role |
| --- | --- | --- |
| `bg` | `#181717` | Main page background |
| `surface` | `#101010` | Primary panels / tiles |
| `surface-2` | `#171717` | Secondary surface steps |
| `surface-3` | `#1d1d1d` | Tertiary surface step |
| `border` | `#2d2d2d` | Hairline separators |
| `border-strong` | `#4a4a4a` | Intentional structure / selected framing |
| `text` | `#f3f3f0` | Primary text |
| `muted` | `#999890` | Secondary labels |
| `muted-2` | `#7a7870` | Low-emphasis helper state |

### Accent colors

| Token | Hex | Role |
| --- | --- | --- |
| `red` | `#d90d16` | Main decisive action / danger |
| `red-hover` | `#f12630` | Hover state for red action |
| `yellow` | `#f3be22` | Calibration accent / selection cue / focus |
| `ok` | `#76d17e` | Success status only |
| `accent-dim` | `#73070d` | Deep red support tone |

### Utility transparencies

| Token | Value | Role |
| --- | --- | --- |
| `yellow-dim` | `rgba(243, 190, 34, 0.16)` | Focus halo / soft highlight |
| `white-line` | `rgba(255, 255, 255, 0.08)` | Hairline highlight |

### Contextual support colors

These are secondary contextual colors, not brand colors for the whole UI.

| Use | Colors | Meaning |
| --- | --- | --- |
| BW badge | `#3a3a3a` to `#1e1e1e` | Graphite silver |
| Color negative badge | `#bd7a2c` to `#8c5821` | Warm amber |
| Slide badge | `#6a5a92` to `#463961` | Cool violet |

### Background treatment

The page background is not flat. It is built from:

- a near-black vertical gradient
- a faint top-left reflected wash
- a subtle fixed matte grain texture

That combination matters. The theme loses credibility if reduced to a plain
solid background.

## Typography

### Primary UI face

- `IBM Plex Sans Condensed`
- fallback direction: `Barlow Condensed`, `Roboto Condensed`, `Arial Narrow`

Use it for:

- headings
- buttons
- labels
- selectors
- main tile titles
- general UI text

Why it works:

- narrow, upright, engineered
- less generic than common SaaS sans stacks
- supports a technical, measured tone without looking retro or militaristic

### Secondary / technical face

- `IBM Plex Mono`

Use it for:

- filenames
- paths
- metadata values
- numbers that should feel measured
- compact technical readouts

### Type rules

- default body size is modest, not oversized
- uppercase is used heavily for labels and controls
- letterspacing is deliberate and frequent
- tabular numerals should be enabled globally where possible
- primary titles are bold through weight and proportion, not through huge size

## Geometry

- default radius: `2px`
- avoid large rounded corners
- avoid pill-heavy styling except for tiny status indicators
- borders matter more than shadows
- panels should feel planar and assembled, not floating

## Surface Language

### What surfaces should look like

- matte
- dense
- slightly layered
- lightly highlighted from the top
- separated by borders more than shadows

### What to avoid

- glassmorphism
- large blurred shadows
- big soft cards
- pure black backgrounds
- bright neon accents
- high-saturation decorative gradients

## Interaction Language

### Selection

Selected objects should feel like they are being **framed** or **calibrated**,
not filled with color. In FilmEx this shows up as yellow corner brackets.

Portable rule:

- prefer edge signals, brackets, ticks, or border shifts
- do not rely on full-surface bright fills for selection

### Primary action hierarchy

The interface should have **one visually dominant action** at a time.

Portable rule:

- keep most controls neutral
- reserve red for the single strongest action
- do not create multiple competing bright buttons

### Hover / focus

Hover and focus should feel precise:

- subtle border strengthening
- slight surface lift via tonal shift
- yellow focus halo where needed
- no bouncy transforms

## Component Translation Rules

If this style is moved to another product, preserve these patterns:

- headers should read like equipment top plates, not website nav bars
- section headings should be small uppercase labels with horizontal rules
- controls should feel engraved/labeled rather than playful
- status should appear as compact instrument readouts
- drawers and side panels should feel like service panels, not modal cards
- lists should prefer contiguous rows and separators over stacks of cards

## Brand Expression Rules

This theme gets identity from restraint. To keep it intact in another site:

- keep the palette mostly neutral
- use yellow sparingly but consistently
- keep red rare and important
- let typography and spacing create personality
- use micro-details like ticks, brackets, strips, and separators instead of
  adding more illustration

## Portable Art Direction

If rebuilding this style elsewhere, the target impression should be:

> A serious analog-photo tool with the visual discipline of a precision camera
> body and the ergonomics of a workshop instrument panel.

Short version:

- condensed industrial typography
- graphite matte surfaces
- yellow calibration accents
- one red action
- square geometry
- minimal shadow
- technical readouts
- restrained motion
- no soft SaaS card aesthetic

## What Is Project-Specific vs Portable

### Keep when porting

- the instrument-first tone
- the condensed + mono type pairing
- the dark graphite palette
- yellow as calibration/selection
- red as singular decisive action
- 2px corner discipline
- contiguous row/list treatment
- quiet, technical interaction design

### Adapt per project

- actual component set
- information architecture
- domain-specific badges and labels
- any film-brand-specific color strip logic
- copy tone and terminology

### Do not copy blindly

- literal camera metaphors where the new product has no physical-tool logic
- film-specific taxonomy colors unless the new product needs them
- exact component proportions if the new content model is substantially different

