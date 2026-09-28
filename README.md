# 🚀 Astra Agent Pack for AI.Duino

**Frontier-AI-level development agents for Arduino IDE 2.x + [AI.Duino](https://github.com/NikolaiRadke/AI.duino) v2.6+**

10 expert-level custom agents that cover the full embedded development lifecycle — from architecture through deployment and security audit. Plus a standalone Python orchestrator that chains everything end-to-end autonomously.

![My Agents 10/20](https://img.shields.io/badge/Agents-10-00C7B7?style=for-the-badge) ![Arduino IDE](https://img.shields.io/badge/Arduino_IDE-2.x-00979D?style=for-the-badge&logo=arduino) ![AI.Duino](https://img.shields.io/badge/AI.Duino-v2.6+-FF6B6B?style=for-the-badge)

---

## What's in the box

### Layer 1: Agent Pack (JSON Import — zero code changes)

| Agent | Purpose |
|-------|---------|
| 🏗️ **Architect** | Full project design from natural-language description — pin assignments, library deps, file structure, scaffold code |
| ⚡ **Implement** | Production-quality code generation with non-blocking patterns, ISR safety, memory awareness |
| 🔍 **Code Review** | Adversarial bug hunter — finds blocking calls, buffer overflows, race conditions, interrupt-safety issues |
| 🐛 **Debug & Fix** | Compiler error diagnosis with root-cause analysis and complete corrected code output |
| 🧪 **Test Generator** | Serial-output test sketches with PASS/FAIL reporting, edge-case coverage, timing assertions |
| 📊 **Memory Optimizer** | SRAM/Flash analysis — finds String waste, missing PROGMEM/F(), struct padding, oversized buffers |
| 🚀 **Deploy Advisor** | Pre-deployment checklist — pin conflicts, power budget, watchdog, OTA readiness, EEPROM wear |
| 📝 **Documentation** | README, ASCII pinout diagrams, wiring guide, API reference generation |
| 🔄 **Refactor** | State-machine extraction, file splitting, FSM conversion, non-blocking refactoring |
| 🛡️ **Security Audit** | WiFi/BLE credential audit, input validation, unencrypted connections, exposed debug endpoints |

Each agent has a deeply engineered system prompt (1.7K–2.8K chars) with:
- Expert persona with specific Arduino/ESP32 domain knowledge
- Structured output format (severity-ranked findings, complete code blocks)
- Platform-specific rules ("never use `String` concatenation in a loop on AVR")
- Appropriate temperature tuning per task type

### Layer 2: Orchestrator (Python CLI — autonomous pipeline)

```
Design → Implement → Compile → Auto-Fix Loop → Upload → Serial Test → Report
```

- Multi-provider AI: OpenAI, Gemini, Anthropic
- `arduino-cli` auto-detection + serial port auto-detect
- Compile-fix convergence tracking (stops if errors aren't decreasing, max 5 rounds)
- Upload with confirmation prompt (never flashes without asking)
- Serial monitor capture + AI-powered test verification
- Full pipeline history JSON + markdown reports

---

## Quick Start

### Install the Agents (30 seconds)

1. Open **Arduino IDE 2.x** with **AI.Duino** installed
2. Open the **Custom AI Agents** panel (`Ctrl+Shift+P` → "Manage Agents")
3. Click **📥 Import**
4. Select `astra_agent_pack.json`
5. Done — 10 agents in your "My Agents" panel

**Or** copy the file directly:
```bash
# macOS/Linux
cp astra_agent_pack.json ~/.aiduino/.aiduino-custom-agents.json

# Windows
copy astra_agent_pack.json %USERPROFILE%\.aiduino\.aiduino-custom-agents.json
```

### Run an Agent

Click **▶** on any agent, or use the command palette:
- `Ctrl+Shift+P` → "Run Agent" → select an agent
- The agent sees your current code + context (board info, libraries, compiler errors, etc.) based on its configured context flags

### Use the Orchestrator (optional)

```bash
# Install dependencies
pip install openai google-genai anthropic pyyaml

# Set your API key
export OPENAI_API_KEY="sk-..."  # or GEMINI_API_KEY / ANTHROPIC_API_KEY

# Full autonomous pipeline
python arduino_astra_orchestrator.py --goal "Build an ESP32 weather station with DHT22 and OLED"

# Just compile + auto-fix an existing sketch
python arduino_astra_orchestrator.py --sketch ./MyProject --phase compile_fix

# Use Gemini instead of OpenAI
python arduino_astra_orchestrator.py --goal "WiFi logger" --provider gemini --model gemini-2.5-flash

# Dry run (no AI calls, no hardware)
python arduino_astra_orchestrator.py --dry-run --goal "Test"
```

Edit `orchestrator_config.yaml` for defaults (board FQBN, baud rate, API provider, pipeline phases, etc.)

---

## Requirements

- **Arduino IDE 2.x** (tested on 2.3.8)
- **AI.Duino v2.6+** (tested on v2.7.0) — [install from GitHub](https://github.com/NikolaiRadke/AI.duino)
- **For Layer 2 only:**
  - Python 3.10+
  - `arduino-cli` on PATH
  - An API key for OpenAI, Gemini, or Anthropic

## Supported Boards

The agent prompts cover both AVR and ESP32 platforms:
- Arduino UNO / Nano / Mega (AVR-specific: SRAM constraints, PROGMEM, avr-size)
- ESP32 / ESP32-S3 / ESP32-C3 (IRAM_ATTR, dual-core, WiFi/BLE, OTA, PSRAM)
- Any board supported by `arduino-cli`

## Files

| File | Description |
|------|-------------|
| `astra_agent_pack.json` | 10 agents — import into AI.Duino |
| `arduino_astra_orchestrator.py` | Autonomous pipeline CLI (~980 lines) |
| `orchestrator_config.yaml` | Pipeline configuration |

## License

MIT

## Credits

Built for use with [AI.Duino](https://github.com/NikolaiRadke/AI.duino) by NikolaiRadke.
