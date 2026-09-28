#!/usr/bin/env python3
"""
Arduino Astra Orchestrator — End-to-End Autonomous Development Pipeline
========================================================================
Chains the full Arduino development lifecycle:
  Design → Implement → Compile → Auto-Fix Loop → Upload → Serial Test → Report

Works alongside AI.Duino v2.7.0 in Arduino IDE 2.3.8, but runs independently
via arduino-cli for the compile/upload/monitor phases.

Usage:
  python arduino_astra_orchestrator.py --goal "Build an ESP32 weather station with DHT22 and OLED"
  python arduino_astra_orchestrator.py --sketch ./MyProject --phase compile_fix
  python arduino_astra_orchestrator.py --dry-run --goal "Blink an LED on pin 13"

Requirements:
  pip install openai google-genai anthropic pyyaml
  arduino-cli installed and on PATH (or configured in orchestrator_config.yaml)
"""

import argparse
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import time
import datetime
from pathlib import Path
from typing import Optional

try:
    import yaml
except ImportError:
    print("ERROR: pyyaml required. Install with: pip install pyyaml")
    sys.exit(1)


# ─── Constants ───────────────────────────────────────────────────────────────

VERSION = "1.0.0"
DEFAULT_CONFIG = "orchestrator_config.yaml"

PHASE_ORDER = [
    "architect",
    "implement",
    "compile_fix",
    "upload",
    "serial_test",
    "report",
]

# ─── Expert System Prompts ───────────────────────────────────────────────────

PROMPTS = {
    "architect": """You are an expert embedded systems architect specializing in Arduino and ESP32 platforms.

Given a project description, produce a complete technical design document with:

1. **Project Overview** — What the system does, key requirements, constraints
2. **Hardware Requirements** — Board selection rationale, sensors, actuators, power supply
3. **Pin Assignment Table** — Every pin used, its purpose, mode (INPUT/OUTPUT/INPUT_PULLUP), notes
4. **Library Dependencies** — Exact library names and recommended versions
5. **File Structure** — Which .ino, .h, .cpp files to create and what each contains
6. **Architecture** — Data flow, state machine design, interrupt strategy, timing budget
7. **Complete Scaffold Code** — The main .ino file with all includes, pin definitions, setup() and loop() structure, function stubs with descriptive comments

Rules:
- Always prefer non-blocking designs (millis()-based timing, state machines)
- For ESP32: use IRAM_ATTR for ISRs, consider dual-core task distribution
- For AVR: be SRAM-conscious (2KB on UNO), prefer PROGMEM for string constants
- Specify exact pin numbers, not just "a digital pin"
- Include power budget estimate (mA per component)

Output the design as structured markdown with code blocks labeled ```cpp.""",

    "implement": """You are a senior embedded firmware engineer writing production-quality Arduino/ESP32 code.

Given a design document, feature description, or existing code to extend, write COMPLETE, COMPILABLE code.

Mandatory engineering standards:
- Non-blocking: Use millis()-based timing. NEVER use delay() in production code except in setup().
- ISR safety: Keep ISRs short. Use volatile for shared variables. Use IRAM_ATTR on ESP32.
- Memory: Use const, PROGMEM, F() macro for string literals in Serial.print(). Avoid String class on AVR.
- Error handling: Check return values from Wire, SPI, Serial. Handle sensor read failures gracefully.
- Include guards: Use #pragma once or #ifndef guards in all .h files.
- Naming: descriptive camelCase for variables, PascalCase for classes, UPPER_SNAKE for constants.
- Structure: setup() for initialization only, loop() delegates to state machine or task functions.

Output the COMPLETE file contents for each file. Use ```cpp code blocks with the filename as a comment on the first line:
```cpp
// filename: MyProject.ino
#include ...
```

Do NOT output snippets or partial code. Every file must compile as-is.""",

    "compile_fix": """You are a compiler error diagnostician for Arduino/ESP32 firmware.

Given C/C++ compiler errors and the source code that produced them:

1. **Diagnose** — Explain each error in plain language. Identify the ROOT CAUSE, not just the symptom.
2. **Fix** — Output the COMPLETE corrected source file(s). Not patches, not diffs — the full file.
3. **Explain** — For each fix, explain WHY it works and what the original mistake was.
4. **Warn** — Flag if the fix might have cascading effects or reveals a deeper design issue.

Common Arduino gotchas to check:
- Missing semicolons after class definitions
- Incorrect #include paths (angle brackets vs quotes)
- Type mismatches (int vs uint8_t, float vs double)
- Missing function prototypes (Arduino auto-generates for .ino but not .cpp)
- ESP32-specific: missing IRAM_ATTR, incorrect partition scheme, WiFi/BLE coexistence issues
- Library version conflicts

Output format: For each file that needs changes, provide the complete file:
```cpp
// filename: MyProject.ino
[complete corrected code]
```""",

    "test_verify": """You are a firmware test engineer analyzing Serial Monitor output from an Arduino/ESP32 device.

Given:
- The source code that's running on the device
- The captured Serial Monitor output

Evaluate:
1. **Boot Success** — Did the device initialize all peripherals? Any error messages?
2. **Functional Tests** — If test output includes PASS/FAIL markers, tally them.
3. **Timing** — Are loop iterations within expected timing bounds?
4. **Error Conditions** — Any watchdog resets, stack overflows, memory corruption indicators?
5. **Anomalies** — Unexpected output, garbled characters (baud mismatch?), missing expected output

Output a structured test report:
- Overall: PASS / FAIL / INCONCLUSIVE
- Individual findings with severity
- Recommendations for improvement""",

    "report": """You are a technical writer summarizing an automated firmware development session.

Given the full pipeline history (design, implementation, compile iterations, test results), produce a concise report:

1. **Summary** — What was built, for which board, key decisions made
2. **Pipeline Results** — How many compile-fix iterations, what errors were found and fixed
3. **Test Results** — Serial monitor findings, pass/fail
4. **Code Quality** — Any remaining warnings, known limitations, TODO items
5. **Recommendations** — Next steps, things to test manually, deployment considerations

Keep it concise and actionable. This is for the developer's reference, not a thesis.""",
}


# ─── Utility Functions ──────────────────────────────────────────────────────

def load_config(config_path: str) -> dict:
    """Load and validate orchestrator configuration."""
    path = Path(config_path)
    if not path.exists():
        logging.warning(f"Config file not found: {config_path}. Using defaults.")
        return get_default_config()

    with open(path, "r", encoding="utf-8") as f:
        config = yaml.safe_load(f) or {}

    # Resolve env vars in api_key
    api_key = config.get("ai", {}).get("api_key", "")
    if api_key.startswith("$"):
        env_var = api_key[1:]
        config["ai"]["api_key"] = os.environ.get(env_var, "")
        if not config["ai"]["api_key"]:
            logging.warning(f"Environment variable {env_var} not set.")

    return config


def get_default_config() -> dict:
    """Return sensible defaults."""
    return {
        "board": {"fqbn": "esp32:esp32:esp32", "port": "", "baud_rate": 115200},
        "ai": {
            "provider": "openai",
            "model": "gpt-4o",
            "api_key": os.environ.get("OPENAI_API_KEY", ""),
            "code_temperature": 0.2,
            "review_temperature": 0.5,
            "max_tokens": 8000,
        },
        "pipeline": {
            "max_fix_iterations": 5,
            "max_pipeline_cycles": 3,
            "confirm_upload": True,
            "serial_capture_seconds": 10,
            "auto_apply_code": True,
            "phases": PHASE_ORDER,
        },
        "paths": {"arduino_cli": "", "sketch_dir": "", "report_dir": "./astra_reports"},
        "logging": {
            "level": "info",
            "log_file": "./astra_reports/orchestrator.log",
            "show_llm_traffic": False,
        },
    }


def find_arduino_cli(config: dict) -> Optional[str]:
    """Find arduino-cli binary."""
    configured = config.get("paths", {}).get("arduino_cli", "")
    if configured and Path(configured).exists():
        return configured

    # Check PATH
    cli = shutil.which("arduino-cli")
    if cli:
        return cli

    # Check common locations
    common_paths = [
        Path.home() / ".local" / "bin" / "arduino-cli",
        Path.home() / "bin" / "arduino-cli",
        Path(r"C:\Program Files\Arduino CLI\arduino-cli.exe"),
        Path(r"C:\Users") / os.environ.get("USERNAME", "") / "AppData" / "Local" / "Programs" / "arduino-cli" / "arduino-cli.exe",
        # Arduino IDE 2.x bundles one
        Path(r"C:\Users") / os.environ.get("USERNAME", "") / "AppData" / "Local" / "Programs" / "arduino-ide" / "resources" / "app" / "lib" / "backend" / "resources" / "arduino-cli.exe",
    ]

    for p in common_paths:
        if p.exists():
            return str(p)

    return None


def detect_port(cli_path: str) -> Optional[str]:
    """Auto-detect connected board port using arduino-cli."""
    try:
        result = subprocess.run(
            [cli_path, "board", "list", "--format", "json"],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0:
            data = json.loads(result.stdout)
            ports = data if isinstance(data, list) else data.get("detected_ports", [])
            for port_info in ports:
                port = port_info.get("port", {})
                addr = port.get("address", "")
                if addr:
                    return addr
    except (subprocess.TimeoutExpired, json.JSONDecodeError, FileNotFoundError):
        pass
    return None


# ─── AI Client ───────────────────────────────────────────────────────────────

class AIClient:
    """Unified client for calling LLM APIs."""

    def __init__(self, config: dict):
        self.provider = config.get("ai", {}).get("provider", "openai")
        self.model = config.get("ai", {}).get("model", "gpt-4o")
        self.api_key = config.get("ai", {}).get("api_key", "")
        self.max_tokens = config.get("ai", {}).get("max_tokens", 8000)
        self.show_traffic = config.get("logging", {}).get("show_llm_traffic", False)

        if not self.api_key:
            raise ValueError(
                f"No API key configured for provider '{self.provider}'. "
                f"Set it in orchestrator_config.yaml or the corresponding env var."
            )

    def call(self, system_prompt: str, user_prompt: str, temperature: float = 0.3) -> str:
        """Call the configured LLM and return the response text."""
        if self.show_traffic:
            logging.debug(f"[LLM Request] system={system_prompt[:200]}... user={user_prompt[:200]}...")

        if self.provider == "openai":
            return self._call_openai(system_prompt, user_prompt, temperature)
        elif self.provider == "gemini":
            return self._call_gemini(system_prompt, user_prompt, temperature)
        elif self.provider == "anthropic":
            return self._call_anthropic(system_prompt, user_prompt, temperature)
        else:
            raise ValueError(f"Unknown AI provider: {self.provider}")

    def _call_openai(self, system_prompt: str, user_prompt: str, temperature: float) -> str:
        try:
            from openai import OpenAI
        except ImportError:
            raise ImportError("openai package required. Install with: pip install openai")

        client = OpenAI(api_key=self.api_key)
        response = client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            temperature=temperature,
            max_tokens=self.max_tokens,
        )
        return response.choices[0].message.content

    def _call_gemini(self, system_prompt: str, user_prompt: str, temperature: float) -> str:
        try:
            from google import genai
            from google.genai import types
        except ImportError:
            raise ImportError("google-genai package required. Install with: pip install google-genai")

        client = genai.Client(api_key=self.api_key)
        response = client.models.generate_content(
            model=self.model,
            contents=user_prompt,
            config=types.GenerateContentConfig(
                system_instruction=system_prompt,
                temperature=temperature,
                max_output_tokens=self.max_tokens,
            ),
        )
        return response.text

    def _call_anthropic(self, system_prompt: str, user_prompt: str, temperature: float) -> str:
        try:
            import anthropic
        except ImportError:
            raise ImportError("anthropic package required. Install with: pip install anthropic")

        client = anthropic.Anthropic(api_key=self.api_key)
        response = client.messages.create(
            model=self.model,
            max_tokens=self.max_tokens,
            system=system_prompt,
            messages=[{"role": "user", "content": user_prompt}],
            temperature=temperature,
        )
        return response.content[0].text


# ─── Code Extractor ──────────────────────────────────────────────────────────

def extract_code_blocks(response: str) -> dict[str, str]:
    """
    Extract code blocks from LLM response.
    Looks for patterns like:
      ```cpp
      // filename: MyProject.ino
      ...code...
      ```
    Returns dict of {filename: code_content}
    """
    blocks = {}
    pattern = r"```(?:cpp|c\+\+|c|arduino|ino)\s*\n(.*?)```"
    matches = re.findall(pattern, response, re.DOTALL)

    for i, code in enumerate(matches):
        # Try to extract filename from first comment line
        filename = None
        lines = code.strip().split("\n")
        if lines:
            first_line = lines[0].strip()
            # Match patterns like: // filename: Foo.ino   or   // Foo.ino   or   /* Foo.cpp */
            fname_match = re.match(
                r"^(?://|/\*)\s*(?:filename:\s*|file:\s*)?(\S+\.(?:ino|cpp|h|c|hpp))",
                first_line, re.IGNORECASE,
            )
            if fname_match:
                filename = fname_match.group(1)
                code = "\n".join(lines[1:])  # Remove the filename comment

        if not filename:
            # Default naming
            if i == 0:
                filename = "sketch.ino"
            else:
                filename = f"module_{i}.cpp"

        blocks[filename] = code.strip()

    return blocks


# ─── Arduino CLI Wrapper ─────────────────────────────────────────────────────

class ArduinoCLI:
    """Wrapper around arduino-cli for compile, upload, and monitor operations."""

    def __init__(self, cli_path: str, fqbn: str, port: str = ""):
        self.cli = cli_path
        self.fqbn = fqbn
        self.port = port

    def compile(self, sketch_dir: str) -> tuple[bool, str, list[str]]:
        """
        Compile a sketch. Returns (success, full_output, error_lines).
        """
        cmd = [self.cli, "compile", "--fqbn", self.fqbn, sketch_dir, "--warnings", "all"]
        logging.info(f"Compiling: {' '.join(cmd)}")

        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=120,
            )
            output = result.stdout + "\n" + result.stderr
            success = result.returncode == 0

            # Extract error lines
            errors = []
            for line in output.split("\n"):
                line_stripped = line.strip()
                if ": error:" in line_stripped or "Error" in line_stripped:
                    errors.append(line_stripped)
                elif ": fatal error:" in line_stripped:
                    errors.append(line_stripped)

            return success, output, errors

        except subprocess.TimeoutExpired:
            return False, "Compilation timed out after 120 seconds", ["Compilation timeout"]
        except FileNotFoundError:
            return False, f"arduino-cli not found at: {self.cli}", ["arduino-cli not found"]

    def upload(self, sketch_dir: str) -> tuple[bool, str]:
        """Upload compiled sketch to board."""
        if not self.port:
            return False, "No port configured or detected"

        cmd = [self.cli, "upload", "--fqbn", self.fqbn, "--port", self.port, sketch_dir]
        logging.info(f"Uploading: {' '.join(cmd)}")

        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
            output = result.stdout + "\n" + result.stderr
            return result.returncode == 0, output
        except subprocess.TimeoutExpired:
            return False, "Upload timed out after 60 seconds"

    def monitor(self, baud_rate: int, duration: int) -> str:
        """Capture serial monitor output for a duration."""
        if not self.port:
            return "[No port configured — cannot capture serial output]"

        cmd = [self.cli, "monitor", "--port", self.port, "--config", f"baudrate={baud_rate}"]
        logging.info(f"Monitoring serial for {duration}s: {' '.join(cmd)}")

        try:
            proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            )
            time.sleep(duration)
            proc.terminate()

            try:
                stdout, stderr = proc.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                stdout, stderr = proc.communicate()

            return stdout if stdout else "[No serial output captured]"

        except FileNotFoundError:
            return "[arduino-cli not found — cannot capture serial output]"
        except Exception as e:
            return f"[Serial monitor error: {e}]"


# ─── Pipeline Engine ─────────────────────────────────────────────────────────

class AstraPipeline:
    """Orchestrates the full development pipeline."""

    def __init__(self, config: dict, goal: str, sketch_dir: str, dry_run: bool = False):
        self.config = config
        self.goal = goal
        self.sketch_dir = Path(sketch_dir).resolve()
        self.dry_run = dry_run
        self.history: list[dict] = []
        self.report_dir = Path(config.get("paths", {}).get("report_dir", "./astra_reports")).resolve()
        self.report_dir.mkdir(parents=True, exist_ok=True)

        # Initialize AI client
        if not dry_run:
            self.ai = AIClient(config)
        else:
            self.ai = None

        # Initialize Arduino CLI
        cli_path = find_arduino_cli(config)
        fqbn = config.get("board", {}).get("fqbn", "esp32:esp32:esp32")
        port = config.get("board", {}).get("port", "")

        if cli_path:
            if not port:
                port = detect_port(cli_path) or ""
                if port:
                    logging.info(f"Auto-detected port: {port}")
            self.arduino = ArduinoCLI(cli_path, fqbn, port)
        else:
            logging.warning("arduino-cli not found. Compile/upload/monitor phases will be skipped.")
            self.arduino = None

        # Pipeline config
        pipeline_cfg = config.get("pipeline", {})
        self.max_fix_iters = pipeline_cfg.get("max_fix_iterations", 5)
        self.confirm_upload = pipeline_cfg.get("confirm_upload", True)
        self.serial_seconds = pipeline_cfg.get("serial_capture_seconds", 10)
        self.auto_apply = pipeline_cfg.get("auto_apply_code", True)
        self.phases = pipeline_cfg.get("phases", PHASE_ORDER)

        self.code_temp = config.get("ai", {}).get("code_temperature", 0.2)
        self.review_temp = config.get("ai", {}).get("review_temperature", 0.5)

    def log_phase(self, phase: str, result: dict):
        """Log a phase result to history."""
        entry = {
            "phase": phase,
            "timestamp": datetime.datetime.now().isoformat(),
            **result,
        }
        self.history.append(entry)
        logging.info(f"[{phase.upper()}] {'✅' if result.get('success', True) else '❌'} {result.get('summary', '')}")

    def run(self):
        """Execute the full pipeline."""
        print(f"\n{'='*70}")
        print(f"  🚀 Arduino Astra Orchestrator v{VERSION}")
        print(f"  Goal: {self.goal}")
        print(f"  Sketch: {self.sketch_dir}")
        print(f"  Board: {self.config.get('board', {}).get('fqbn', 'unknown')}")
        print(f"  Dry run: {self.dry_run}")
        print(f"{'='*70}\n")

        if self.dry_run:
            print("[DRY RUN] Simulating pipeline without AI calls or hardware operations.\n")

        try:
            for phase in self.phases:
                if phase not in PHASE_ORDER:
                    logging.warning(f"Unknown phase: {phase}. Skipping.")
                    continue

                print(f"\n{'─'*50}")
                print(f"  Phase: {phase.upper()}")
                print(f"{'─'*50}")

                method = getattr(self, f"_phase_{phase}", None)
                if method:
                    method()
                else:
                    logging.warning(f"Phase '{phase}' not implemented.")

        except KeyboardInterrupt:
            print("\n\n⚠️  Pipeline interrupted by user.")
        except Exception as e:
            logging.error(f"Pipeline error: {e}", exc_info=True)
            print(f"\n❌ Pipeline error: {e}")
        finally:
            self._save_history()
            print(f"\n{'='*70}")
            print(f"  Pipeline complete. History saved to: {self.report_dir}")
            print(f"{'='*70}\n")

    # ── Phase Implementations ────────────────────────────────────────────

    def _phase_architect(self):
        """Design phase: generate project architecture."""
        if self.dry_run:
            self.log_phase("architect", {"success": True, "summary": "[DRY RUN] Would generate architecture"})
            return

        board_info = self.config.get("board", {}).get("fqbn", "unknown")
        prompt = f"""Design an Arduino/ESP32 project for the following goal:

**Goal:** {self.goal}

**Target Board:** {board_info}

Please provide the complete architecture as specified in your instructions."""

        response = self.ai.call(PROMPTS["architect"], prompt, temperature=self.review_temp)

        # Save architecture document
        arch_file = self.report_dir / "architecture.md"
        arch_file.write_text(response, encoding="utf-8")

        self.log_phase("architect", {
            "success": True,
            "summary": f"Architecture document generated ({len(response)} chars)",
            "output_file": str(arch_file),
        })
        print(f"  📄 Architecture saved to: {arch_file}")

    def _phase_implement(self):
        """Implementation phase: generate code from architecture."""
        if self.dry_run:
            self.log_phase("implement", {"success": True, "summary": "[DRY RUN] Would generate code"})
            return

        # Read architecture if available
        arch_file = self.report_dir / "architecture.md"
        context = ""
        if arch_file.exists():
            context = f"\n\n## Architecture Document:\n{arch_file.read_text(encoding='utf-8')}"

        # Read existing code if any
        existing_code = self._read_sketch_files()
        if existing_code:
            context += f"\n\n## Existing Code:\n{existing_code}"

        prompt = f"""Implement the following Arduino/ESP32 project:

**Goal:** {self.goal}

**Target Board:** {self.config.get('board', {}).get('fqbn', 'unknown')}
{context}

Generate COMPLETE, COMPILABLE code files. Each file must have a filename comment on the first line."""

        response = self.ai.call(PROMPTS["implement"], prompt, temperature=self.code_temp)

        # Extract and save code files
        code_blocks = extract_code_blocks(response)

        if code_blocks and self.auto_apply:
            self.sketch_dir.mkdir(parents=True, exist_ok=True)
            for filename, code in code_blocks.items():
                filepath = self.sketch_dir / filename
                filepath.write_text(code, encoding="utf-8")
                print(f"  💾 Written: {filepath}")

        self.log_phase("implement", {
            "success": len(code_blocks) > 0,
            "summary": f"Generated {len(code_blocks)} file(s): {', '.join(code_blocks.keys())}",
            "files": list(code_blocks.keys()),
        })

    def _phase_compile_fix(self):
        """Compile-fix loop: compile, diagnose errors, fix, repeat."""
        if not self.arduino:
            self.log_phase("compile_fix", {"success": False, "summary": "arduino-cli not available"})
            return

        if self.dry_run:
            self.log_phase("compile_fix", {"success": True, "summary": "[DRY RUN] Would compile and fix"})
            return

        defect_counts = []  # Track convergence

        for iteration in range(1, self.max_fix_iters + 1):
            print(f"\n  🔨 Compile iteration {iteration}/{self.max_fix_iters}...")

            success, output, errors = self.arduino.compile(str(self.sketch_dir))
            defect_counts.append(len(errors))

            if success:
                print(f"  ✅ Compilation successful on iteration {iteration}!")
                self.log_phase("compile_fix", {
                    "success": True,
                    "summary": f"Compiled successfully on iteration {iteration}",
                    "iterations": iteration,
                    "defect_trend": defect_counts,
                })
                return

            print(f"  ❌ {len(errors)} error(s) found. Asking AI to fix...")

            # Check convergence: if errors aren't decreasing, escalate
            if iteration >= 3 and len(defect_counts) >= 2:
                if defect_counts[-1] >= defect_counts[-2]:
                    print(f"  ⚠️  Errors not converging (trend: {defect_counts}). Stopping fix loop.")
                    self.log_phase("compile_fix", {
                        "success": False,
                        "summary": f"Not converging after {iteration} iterations. Errors: {defect_counts}",
                        "iterations": iteration,
                        "defect_trend": defect_counts,
                        "last_errors": errors[:10],
                    })
                    return

            # Read current code
            current_code = self._read_sketch_files()

            fix_prompt = f"""The following Arduino code failed to compile.

**Compiler Errors:**
```
{chr(10).join(errors[:20])}
```

**Full Compiler Output:**
```
{output[-3000:]}
```

**Current Source Code:**
{current_code}

Fix ALL errors and output the COMPLETE corrected file(s)."""

            response = self.ai.call(PROMPTS["compile_fix"], fix_prompt, temperature=self.code_temp)

            # Apply fixes
            code_blocks = extract_code_blocks(response)
            if code_blocks and self.auto_apply:
                for filename, code in code_blocks.items():
                    filepath = self.sketch_dir / filename
                    filepath.write_text(code, encoding="utf-8")
                    print(f"  🔧 Fixed: {filepath}")
            else:
                print("  ⚠️  No code blocks extracted from AI response.")
                break

        self.log_phase("compile_fix", {
            "success": False,
            "summary": f"Failed to compile after {self.max_fix_iters} iterations",
            "iterations": self.max_fix_iters,
            "defect_trend": defect_counts,
        })

    def _phase_upload(self):
        """Upload compiled firmware to board."""
        if not self.arduino:
            self.log_phase("upload", {"success": False, "summary": "arduino-cli not available"})
            return

        if self.dry_run:
            self.log_phase("upload", {"success": True, "summary": "[DRY RUN] Would upload to board"})
            return

        # Check if we had a successful compile
        compile_phase = next(
            (h for h in reversed(self.history) if h["phase"] == "compile_fix"),
            None,
        )
        if compile_phase and not compile_phase.get("success"):
            print("  ⚠️  Skipping upload — last compile was not successful.")
            self.log_phase("upload", {"success": False, "summary": "Skipped — compile failed"})
            return

        if self.confirm_upload:
            print(f"\n  ⚡ Ready to upload to {self.arduino.port or 'auto-detect'}")
            response = input("  Proceed with upload? [y/N]: ").strip().lower()
            if response not in ("y", "yes"):
                print("  Upload cancelled by user.")
                self.log_phase("upload", {"success": False, "summary": "Cancelled by user"})
                return

        success, output = self.arduino.upload(str(self.sketch_dir))

        self.log_phase("upload", {
            "success": success,
            "summary": "Upload successful" if success else f"Upload failed: {output[:200]}",
        })

        if success:
            print("  ✅ Upload successful!")
        else:
            print(f"  ❌ Upload failed: {output[:200]}")

    def _phase_serial_test(self):
        """Capture serial output and verify behavior."""
        if not self.arduino:
            self.log_phase("serial_test", {"success": False, "summary": "arduino-cli not available"})
            return

        if self.dry_run:
            self.log_phase("serial_test", {"success": True, "summary": "[DRY RUN] Would capture serial"})
            return

        # Check upload status
        upload_phase = next(
            (h for h in reversed(self.history) if h["phase"] == "upload"),
            None,
        )
        if upload_phase and not upload_phase.get("success"):
            print("  ⚠️  Skipping serial test — upload was not successful.")
            self.log_phase("serial_test", {"success": False, "summary": "Skipped — upload failed"})
            return

        baud = self.config.get("board", {}).get("baud_rate", 115200)
        print(f"  📡 Capturing serial output at {baud} baud for {self.serial_seconds}s...")

        serial_output = self.arduino.monitor(baud, self.serial_seconds)

        # Save raw serial output
        serial_file = self.report_dir / "serial_output.txt"
        serial_file.write_text(serial_output, encoding="utf-8")

        # Ask AI to analyze
        current_code = self._read_sketch_files()
        analysis_prompt = f"""Analyze this Serial Monitor output from an Arduino/ESP32 device.

**Source Code:**
{current_code}

**Serial Output ({self.serial_seconds}s capture):**
```
{serial_output}
```

Provide your test report as specified in your instructions."""

        analysis = self.ai.call(PROMPTS["test_verify"], analysis_prompt, temperature=self.review_temp)

        # Save analysis
        analysis_file = self.report_dir / "test_analysis.md"
        analysis_file.write_text(analysis, encoding="utf-8")

        # Determine pass/fail from analysis
        is_pass = "PASS" in analysis.upper() and "FAIL" not in analysis.upper().split("OVERALL")[0] if "OVERALL" in analysis.upper() else "PASS" in analysis.upper()

        self.log_phase("serial_test", {
            "success": is_pass,
            "summary": f"Test {'PASSED' if is_pass else 'FAILED/INCONCLUSIVE'} — see {analysis_file}",
        })

    def _phase_report(self):
        """Generate final pipeline report."""
        if self.dry_run:
            print("  [DRY RUN] Pipeline summary:")
            for entry in self.history:
                status = "✅" if entry.get("success") else "❌"
                print(f"    {status} {entry['phase']}: {entry.get('summary', '')}")
            self.log_phase("report", {"success": True, "summary": "[DRY RUN] Report generated"})
            return

        history_text = json.dumps(self.history, indent=2, default=str)

        report_prompt = f"""Generate a development session report for:

**Goal:** {self.goal}
**Board:** {self.config.get('board', {}).get('fqbn', 'unknown')}

**Pipeline History:**
```json
{history_text}
```

Provide a concise, actionable report as specified in your instructions."""

        report = self.ai.call(PROMPTS["report"], report_prompt, temperature=self.review_temp)

        report_file = self.report_dir / "pipeline_report.md"
        report_file.write_text(report, encoding="utf-8")

        print(f"\n  📋 Final report: {report_file}")

        self.log_phase("report", {
            "success": True,
            "summary": f"Report generated at {report_file}",
        })

    # ── Helpers ──────────────────────────────────────────────────────────

    def _read_sketch_files(self) -> str:
        """Read all sketch files from the sketch directory."""
        if not self.sketch_dir.exists():
            return ""

        content_parts = []
        extensions = {".ino", ".cpp", ".c", ".h", ".hpp"}

        for filepath in sorted(self.sketch_dir.iterdir()):
            if filepath.suffix.lower() in extensions:
                try:
                    code = filepath.read_text(encoding="utf-8")
                    content_parts.append(f"```cpp\n// filename: {filepath.name}\n{code}\n```")
                except Exception:
                    pass

        return "\n\n".join(content_parts)

    def _save_history(self):
        """Save pipeline history to JSON."""
        history_file = self.report_dir / "pipeline_history.json"
        with open(history_file, "w", encoding="utf-8") as f:
            json.dump(
                {
                    "goal": self.goal,
                    "config": {
                        "board": self.config.get("board", {}),
                        "ai": {
                            "provider": self.config.get("ai", {}).get("provider"),
                            "model": self.config.get("ai", {}).get("model"),
                        },
                    },
                    "phases": self.history,
                    "completed_at": datetime.datetime.now().isoformat(),
                },
                f,
                indent=2,
                default=str,
            )


# ─── CLI Entry Point ─────────────────────────────────────────────────────────

def main():
    # Fix Windows terminal encoding for emoji/unicode output
    if sys.platform == "win32":
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError):
            pass  # Older Python or non-reconfigurable stream

    parser = argparse.ArgumentParser(
        description="Arduino Astra Orchestrator — End-to-End Autonomous Development Pipeline",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s --goal "Build an ESP32 weather station with DHT22 and OLED display"
  %(prog)s --sketch ./MyProject --phase compile_fix
  %(prog)s --dry-run --goal "Blink LED on pin 13"
  %(prog)s --goal "WiFi temperature logger" --provider gemini --model gemini-2.5-flash
        """,
    )

    parser.add_argument("--goal", "-g", type=str, help="Project goal / description")
    parser.add_argument("--sketch", "-s", type=str, default="./sketch", help="Sketch directory (default: ./sketch)")
    parser.add_argument("--config", "-c", type=str, default=DEFAULT_CONFIG, help="Config file path")
    parser.add_argument("--phase", "-p", type=str, nargs="+", choices=PHASE_ORDER, help="Run specific phase(s) only")
    parser.add_argument("--dry-run", "-n", action="store_true", help="Simulate without AI calls or hardware ops")
    parser.add_argument("--provider", type=str, choices=["openai", "gemini", "anthropic"], help="Override AI provider")
    parser.add_argument("--model", type=str, help="Override AI model")
    parser.add_argument("--fqbn", type=str, help="Override board FQBN")
    parser.add_argument("--port", type=str, help="Override serial port")
    parser.add_argument("--no-upload", action="store_true", help="Skip upload phase")
    parser.add_argument("--verbose", "-v", action="store_true", help="Verbose logging")
    parser.add_argument("--version", action="version", version=f"%(prog)s {VERSION}")

    args = parser.parse_args()

    # Require a goal
    if not args.goal and not args.phase:
        parser.error("--goal is required (or use --phase to run specific phases on existing code)")

    # Load config
    config = load_config(args.config)

    # Apply CLI overrides
    if args.provider:
        config.setdefault("ai", {})["provider"] = args.provider
    if args.model:
        config.setdefault("ai", {})["model"] = args.model
    if args.fqbn:
        config.setdefault("board", {})["fqbn"] = args.fqbn
    if args.port:
        config.setdefault("board", {})["port"] = args.port
    if args.phase:
        config.setdefault("pipeline", {})["phases"] = args.phase
    if args.no_upload and "upload" in config.get("pipeline", {}).get("phases", []):
        config["pipeline"]["phases"].remove("upload")

    # Setup logging
    log_level = logging.DEBUG if args.verbose else getattr(
        logging, config.get("logging", {}).get("level", "info").upper(), logging.INFO
    )
    log_file = config.get("logging", {}).get("log_file", "")
    handlers = [logging.StreamHandler()]
    if log_file:
        Path(log_file).parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(log_file, encoding="utf-8"))

    logging.basicConfig(
        level=log_level,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
        handlers=handlers,
    )

    # Run pipeline
    goal = args.goal or f"Process existing sketch in {args.sketch}"
    pipeline = AstraPipeline(config, goal, args.sketch, dry_run=args.dry_run)
    pipeline.run()


if __name__ == "__main__":
    main()
