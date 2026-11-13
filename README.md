# Cyberpunk combat tracker

A web app for the game master: fighters stand on a grid, and the tracker
rolls the dice and works out everything the rulebook asks you to count by
hand: range and cover, SP and armour wear, critical injuries, conditions,
grenades, quickhacks and death checks. Every roll goes into the log.

Django and plain JavaScript, no build step.

## Run it

```bash
python -m venv .venv
.venv\Scripts\activate          # macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
python manage.py migrate
python manage.py seed_demo      # a ready-made scene, "Watson warehouse"
python manage.py runserver
```

Tests: `python manage.py test combat`.

## What the rules cover

Numbers are rulebook sections.

- **§1 Checks.** Stat + d10 ≥ DV, a tie succeeds. A natural 10 adds a d10, a
  natural 1 subtracts one. There are no skills. LUCK: 1–3 points spent for
  +1 each; the pool refills when the fighter is edited (a new session), not
  when the fight is reset.
- **§2 Classes.** Gunner: +2 with the chosen category and one reroll of a
  miss per turn. Cold-blooded killer: +1d6 on the first strike against each
  target, +4 m after a kill (once per round). Netrunner: +2 RAM with a deck,
  the first quickhack each turn costs 1 less.
- **§3 Turns.** Initiative by REF (with boosted reflexes), ties broken with a
  d6 and rerolled. One action and 8 m of movement; movement can be split
  before and after the action.
- **§4 Hitting.** REF + d10 against 12 + range + cover (partial +2, full
  blocks the shot). Class, weapon and implant bonuses add up to +4 at most.
  Dodging: one attempt per round, against bullets only with REF 8+, against
  melee for anyone.
- **§5 Damage.** Dice − SP; a hit through armour removes 1 SP. Melee uses half
  SP (a mono-katana a quarter), then "ignores N". Headshots ×2 after SP, an arm
  hit drops the item, a leg hit halves speed. A charged TECH headshot blinds or
  kills (except bosses).
- **§6 Injuries.** Two sixes + getting through armour → d6 on the table. At
  0 HP: unconscious and BODY + d10 against 12 at the start of own turn; a
  medkit heals 2d6, two per target per fight at most. Stabilising and stopping
  bleeding: TECH against 12. After the fight half of the lost SP comes back.
- **§7–9 Weapons.** Every model from the tables, ammo per die for automatic
  weapons, overclocking (two attacks or a jam), Smart lock-on, TECH charge,
  melee and iconic weapons with their own properties, the Arrow Valve with
  its blast.
- **§10 Grenades.** Up to 20 m, a miss drifts ≈3 m, no dodging and no
  critical injuries, full cover protects.
- **§11 Netrunning.** Cyberdecks and regular quickhacks: RAM is spent even on
  a failure, DV 12/14/16, taking RAM from a body.

## Map and walls

The Walls button above the map turns on the editor: click or drag to build a
wall, drag over a wall to remove it. It works during a fight too and costs no
turn. Walls block movement (paths go around them, and you can't cut a corner
between two walls), block line of fire and melee, and protect from blasts
like full cover (§10). A charged TECH shot goes through one wall (§8). Line
of fire is a segment between square centres; the same function lives in
`rules.py` and `board.js`, so the highlighted targets match what the server
allows.

Shots, strikes, grenades, quickhacks, damage and healing are animated
(`fx.js`). With "reduce motion" turned on in the system, animations are off.

## Left to the GM

As in §18 of the book: magazines and overclock DVs for most models (the
fighter sheet has starting values to adjust), the direction a missed grenade
drifts (the tracker rolls a d8), and enemy stats. The GM can change cover at
any time right in the initiative table.

Not done yet: demons and legendary demons (§12), deep hacking and TRACE
(§13), active implants such as Sandevistan and Berserk, chrome and
cyberpsychosis (§14), and turrets as a separate type. Damage from quickhacks
doesn't cause critical injuries; the book doesn't say so directly, so this is
the tracker's call.

## Structure

```
combat/
  rules.py      tables and dice, no Django
  services.py   combat flow: checks, conditions, log
  models.py     Encounter, Combatant, LogEntry
  views.py      pages and one JSON endpoint for actions
  static/combat/js/board.js   map and turn sheet
  tests.py      including the §16 example
```

The browser never rolls dice: it sends an action, the server checks it and
returns the new state.
