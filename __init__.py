# -*- coding: utf-8 -*-
# Anki AI Field Translator Add-on
# Copyright (c) 2025
# Original author: Josscii
# Fork and modifications: V2power
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

from PyQt6.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
                            QComboBox, QLineEdit, QTabWidget, QWidget, QProgressBar,
                            QTextEdit, QCheckBox, QMessageBox, QGroupBox, QFormLayout,
                            QSpinBox, QDoubleSpinBox, QListWidget, QListWidgetItem)
from PyQt6.QtCore import Qt, QThread, pyqtSignal
import hashlib
import json
import os
import requests
import time
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from aqt import mw
from aqt.utils import qconnect
from aqt.qt import *

# Configuration
addon_path = os.path.dirname(__file__)
legacy_config_path = os.path.join(addon_path, "config.json")

default_config = {
    "api_base_url": "https://generativelanguage.googleapis.com/v1beta",
    "api_key": "",
    "model": "gemini-2.0-flash",
    "openai_fallback_enabled": True,
    "openai_api_key": "",
    "openai_model": "gpt-5-mini",
    "field_mappings": [],
    "processed_notes_cache": {},
    "translation_cache": {},
    "system_prompt": "You are a helpful translator. Translate the text exactly without adding or omitting information. Only respond with the translated text, no explanations or additional content.",
    "max_tokens": 1000,
    "temperature": 0.3,
    "request_delay": 0.5
}
TRANSLATION_CACHE_VERSION = "provider-fallback-v1"
PROCESSING_CACHE_VERSION = "processing-v2"

def _read_json_file(path):
    if not os.path.exists(path):
        return {}

    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}

def _normalize_config(config):
    normalized = default_config.copy()
    normalized.update(config or {})

    if not isinstance(normalized.get("field_mappings"), list):
        normalized["field_mappings"] = []
    if not isinstance(normalized.get("processed_notes_cache"), dict):
        normalized["processed_notes_cache"] = {}
    if not isinstance(normalized.get("translation_cache"), dict):
        normalized["translation_cache"] = {}
    normalized["openai_fallback_enabled"] = bool(normalized.get("openai_fallback_enabled", True))

    return normalized

def load_config():
    legacy_config = _read_json_file(legacy_config_path)
    stored_config = mw.addonManager.getConfig(__name__) or {}

    if "processed_notes_cache" not in stored_config and "processed_notes_cache" in legacy_config:
        stored_config["processed_notes_cache"] = legacy_config["processed_notes_cache"]
    if "translation_cache" not in stored_config and "translation_cache" in legacy_config:
        stored_config["translation_cache"] = legacy_config["translation_cache"]

    merged_config = legacy_config.copy()
    merged_config.update(stored_config)
    return _normalize_config(merged_config)

def save_config(config):
    mw.addonManager.writeConfig(__name__, _normalize_config(config))

def mapping_cache_key(mapping):
    return "||".join([
        PROCESSING_CACHE_VERSION,
        mapping["note_type"],
        mapping["source_field"],
        mapping["target_field"],
        mapping.get("target_language", "English"),
        "1" if mapping.get("skip_if_target_filled", False) else "0",
    ])

def get_model_id_by_name(note_type):
    model = next((m for m in mw.col.models.all() if m["name"] == note_type), None)
    return model["id"] if model else None

def get_note_mod_times(model_id):
    return {
        note_id: mod
        for note_id, mod in mw.col.db.all("select id, mod from notes where mid = ?", model_id)
    }

class TranslationThread(QThread):
    progress_updated = pyqtSignal(int)
    log_message = pyqtSignal(str)
    translation_finished = pyqtSignal()
    
    def __init__(self, config, note_ids, parent=None):
        super().__init__(parent)
        self.config = config
        self.note_ids = note_ids
        self.should_stop = False
        self.last_request_at = 0.0
        self.session = self._build_session()

    def _build_session(self):
        session = requests.Session()
        retry = Retry(
            total=2,
            connect=2,
            read=2,
            backoff_factor=1,
            # A response error should immediately let the other provider try.
            # In particular, do not retry a quota/rate-limit (429) response.
            status_forcelist=(),
            allowed_methods=frozenset(["POST"]),
            raise_on_status=False,
        )
        adapter = HTTPAdapter(max_retries=retry, pool_connections=4, pool_maxsize=4)
        session.mount("https://", adapter)
        session.mount("http://", adapter)
        return session

    def _interruptible_sleep(self, seconds):
        deadline = time.monotonic() + max(0.0, seconds)
        while not self.should_stop and time.monotonic() < deadline:
            time.sleep(min(0.1, deadline - time.monotonic()))
        return not self.should_stop

    def _wait_for_request_slot(self):
        delay = float(self.config.get("request_delay", 0.5) or 0.0)
        if delay <= 0:
            return True

        elapsed = time.monotonic() - self.last_request_at
        if elapsed >= delay:
            return True

        return self._interruptible_sleep(delay - elapsed)

    def _translation_cache_key(self, text, target_language):
        cache_source = "||".join([
            TRANSLATION_CACHE_VERSION,
            self.config.get("api_base_url", ""),
            self.config.get("model", ""),
            str(self.config.get("openai_fallback_enabled", True)),
            self.config.get("openai_model", ""),
            self.config.get("system_prompt", ""),
            str(self.config.get("temperature", 0.3)),
            str(self.config.get("max_tokens", 1000)),
            target_language,
            text,
        ])
        return hashlib.sha256(cache_source.encode("utf-8")).hexdigest()

    def _get_cached_translation(self, text, target_language):
        cache = self.config.setdefault("translation_cache", {})
        return cache.get(self._translation_cache_key(text, target_language))

    def _set_cached_translation(self, text, target_language, translated_text):
        cache = self.config.setdefault("translation_cache", {})
        cache[self._translation_cache_key(text, target_language)] = translated_text
        while len(cache) > 5000:
            cache.pop(next(iter(cache)))

    def _extract_response_text(self, response_json):
        for candidate in response_json.get("candidates", []):
            parts = candidate.get("content", {}).get("parts", [])
            text_parts = [part.get("text", "") for part in parts if part.get("text")]
            if text_parts:
                return "".join(text_parts).strip()

        prompt_feedback = response_json.get("promptFeedback", {})
        block_reason = prompt_feedback.get("blockReason")
        if block_reason:
            self.log_message.emit(f"Gemini blocked the prompt: {block_reason}")

        return None

    def _extract_openai_response_text(self, response_json):
        output_text = response_json.get("output_text")
        if output_text:
            return output_text.strip()

        text_parts = []
        for item in response_json.get("output", []):
            for content in item.get("content", []):
                if content.get("type") == "output_text" and content.get("text"):
                    text_parts.append(content["text"])
        return "".join(text_parts).strip() or None

    def _build_translation_prompts(self, text, target_language):
        resolved_target_language = "Brazilian Portuguese" if target_language == "Portuguese" else target_language
        system_prompt = (
            f"{self.config['system_prompt']} "
            f"You must translate the provided text into {resolved_target_language}. "
            "Do not rewrite, paraphrase, normalize, or correct the source language. "
            "Keep line breaks, punctuation, and emphasis when possible. "
            "Return only the translation."
        )
        user_prompt = (
            f"Translate the following text into {resolved_target_language}.\n"
            "Return only the translated text.\n\n"
            "TEXT TO TRANSLATE:\n"
            f"{text}"
        )
        return system_prompt, user_prompt

    def _translate_with_gemini(self, system_prompt, user_prompt):
        api_key = self.config.get("api_key", "").strip()
        base_url = self.config.get("api_base_url", "").rstrip("/")
        model_name = self.config.get("model", "").removeprefix("models/")
        if not api_key or not base_url or not model_name:
            self.log_message.emit("Gemini is not configured; trying OpenAI fallback.")
            return None

        payload = {
            "system_instruction": {"parts": [{"text": system_prompt}]},
            "contents": [{"role": "user", "parts": [{"text": user_prompt}]}],
            "generationConfig": {
                "temperature": self.config.get("temperature", 0.3),
                "maxOutputTokens": self.config.get("max_tokens", 1000),
            },
        }
        try:
            response = self.session.post(
                f"{base_url}/models/{model_name}:generateContent",
                headers={"Content-Type": "application/json"},
                params={"key": api_key},
                json=payload,
                timeout=(10, 60),
            )
            self.last_request_at = time.monotonic()
            if response.status_code == 200:
                translated_text = self._extract_response_text(response.json())
                if translated_text:
                    return translated_text
                self.log_message.emit("Gemini returned an empty response; trying OpenAI fallback.")
            else:
                self.log_message.emit(
                    f"Gemini failed with HTTP {response.status_code}; trying OpenAI fallback."
                )
        except (requests.RequestException, ValueError) as e:
            self.log_message.emit(f"Gemini request failed ({e}); trying OpenAI fallback.")
        return None

    def _translate_with_openai(self, system_prompt, user_prompt):
        if not self.config.get("openai_fallback_enabled", True):
            return None

        api_key = self.config.get("openai_api_key", "").strip()
        model = self.config.get("openai_model", "").strip()
        if not api_key or not model:
            self.log_message.emit("OpenAI fallback is not configured.")
            return None

        payload = {
            "model": model,
            "instructions": system_prompt,
            "input": user_prompt,
            "max_output_tokens": self.config.get("max_tokens", 1000),
            "store": False,
        }
        try:
            response = self.session.post(
                "https://api.openai.com/v1/responses",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
                timeout=(10, 60),
            )
            self.last_request_at = time.monotonic()
            if response.status_code == 200:
                translated_text = self._extract_openai_response_text(response.json())
                if translated_text:
                    self.log_message.emit(f"Translated using OpenAI fallback ({model}).")
                    return translated_text
                self.log_message.emit("OpenAI returned an empty response.")
            else:
                self.log_message.emit(f"OpenAI fallback failed with HTTP {response.status_code}.")
        except (requests.RequestException, ValueError) as e:
            self.log_message.emit(f"OpenAI fallback request failed: {e}")
        return None
        
    def run(self):
        total_notes = len(self.note_ids)
        processed = 0
        mappings_by_note_type = {}

        for mapping in self.config["field_mappings"]:
            mappings_by_note_type.setdefault(mapping["note_type"], []).append(mapping)

        try:
            for note_id in self.note_ids:
                if self.should_stop:
                    self.log_message.emit("Translation stopped by user.")
                    break

                note = mw.col.get_note(note_id)
                was_modified = False
                note_type = note.note_type()["name"]
                note_mappings = mappings_by_note_type.get(note_type, [])
                processed_mapping_keys = set()

                for mapping in note_mappings:
                    source_field = mapping["source_field"]
                    target_field = mapping["target_field"]
                    cache_key = mapping_cache_key(mapping)
                    cache_bucket = self.config.setdefault("processed_notes_cache", {}).setdefault(cache_key, {})

                    if source_field in note and target_field in note:
                        source_content = note[source_field]

                        # Skip if source field is empty or target field already has content
                        if not source_content.strip():
                            cache_bucket[str(note_id)] = note.mod
                            processed_mapping_keys.add(cache_key)
                            continue

                        if mapping.get("skip_if_target_filled", False) and note[target_field].strip():
                            self.log_message.emit(f"Skipping note {note_id} as target field '{target_field}' already has content")
                            cache_bucket[str(note_id)] = note.mod
                            processed_mapping_keys.add(cache_key)
                            continue

                        try:
                            target_language = mapping.get("target_language", "English")
                            translated_text = self._get_cached_translation(source_content, target_language)
                            if translated_text is None:
                                translated_text = self.translate_text(source_content, target_language)
                                if translated_text:
                                    self._set_cached_translation(source_content, target_language, translated_text)

                            if translated_text:
                                note[target_field] = translated_text
                                was_modified = True
                                processed_mapping_keys.add(cache_key)
                                self.log_message.emit(f"Translated note {note_id} from '{source_field}' to '{target_field}' in {target_language}")
                        except Exception as e:
                            self.log_message.emit(f"Error translating note {note_id}: {str(e)}")
                            self._interruptible_sleep(2)

                if was_modified:
                    note.flush()

                current_mod = note.mod

                for mapping in note_mappings:
                    cache_key = mapping_cache_key(mapping)
                    if cache_key not in processed_mapping_keys:
                        continue
                    cache_bucket = self.config.setdefault("processed_notes_cache", {}).setdefault(cache_key, {})
                    cache_bucket[str(note_id)] = current_mod

                processed += 1
                self.progress_updated.emit(int(processed / total_notes * 100))
        except Exception as e:
            self.log_message.emit(f"Unexpected translation error: {str(e)}")
        finally:
            self.session.close()
            self.translation_finished.emit()
    
    def translate_text(self, text, target_language):
        if self.should_stop:
            return None

        if not self._wait_for_request_slot():
            return None

        system_prompt, user_prompt = self._build_translation_prompts(text, target_language)
        translated_text = self._translate_with_gemini(system_prompt, user_prompt)
        if translated_text:
            return translated_text
        return self._translate_with_openai(system_prompt, user_prompt)
    
    def stop(self):
        self.should_stop = True

class FieldMappingDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Field Mapping")
        self.resize(400, 350)
        
        self.note_types = self.get_note_types()
        self.current_note_type = None
        self.current_fields = []
        self.target_languages = [
            "Chinese", "English", "Spanish", "French", "German", 
            "Japanese", "Korean", "Russian", "Italian", "Portuguese",
            "Arabic", "Hindi", "Dutch", "Swedish", "Turkish",
            "Polish", "Vietnamese", "Thai", "Indonesian", "Hebrew",
            "Greek", "Czech", "Danish", "Finnish", "Norwegian"
        ]
        
        self.setup_ui()
    
    def setup_ui(self):
        layout = QVBoxLayout(self)
        
        # Note type selection
        form_layout = QFormLayout()
        self.note_type_combo = QComboBox()
        self.note_type_combo.addItems(sorted(self.note_types.keys()))
        qconnect(self.note_type_combo.currentIndexChanged, self.on_note_type_changed)
        form_layout.addRow("Note Type:", self.note_type_combo)
        
        # Source and target field selection
        self.source_field_combo = QComboBox()
        self.target_field_combo = QComboBox()
        form_layout.addRow("Source Field:", self.source_field_combo)
        form_layout.addRow("Target Field:", self.target_field_combo)
        
        # Target language selection
        self.target_language_combo = QComboBox()
        self.target_language_combo.addItems(sorted(self.target_languages))
        form_layout.addRow("Target Language:", self.target_language_combo)
        
        # Skip if target filled checkbox
        self.skip_if_filled_checkbox = QCheckBox("Skip if target field already has content")
        self.skip_if_filled_checkbox.setChecked(True)
        
        layout.addLayout(form_layout)
        layout.addWidget(self.skip_if_filled_checkbox)
        
        # Buttons
        button_layout = QHBoxLayout()
        self.add_btn = QPushButton("Add Mapping")
        self.cancel_btn = QPushButton("Cancel")
        
        button_layout.addWidget(self.add_btn)
        button_layout.addWidget(self.cancel_btn)
        
        qconnect(self.add_btn.clicked, self.accept)
        qconnect(self.cancel_btn.clicked, self.reject)
        
        layout.addLayout(button_layout)
        
        if self.note_type_combo.count() > 0:
            self.on_note_type_changed(0)
    
    def get_note_types(self):
        result = {}
        for model in mw.col.models.all():
            field_names = [field["name"] for field in model["flds"]]
            result[model["name"]] = field_names
        return result
    
    def on_note_type_changed(self, index):
        if index < 0:
            return
            
        self.current_note_type = self.note_type_combo.currentText()
        self.current_fields = self.note_types[self.current_note_type]
        
        self.source_field_combo.clear()
        self.target_field_combo.clear()
        
        self.source_field_combo.addItems(self.current_fields)
        self.target_field_combo.addItems(self.current_fields)
    
    def get_mapping(self):
        return {
            "note_type": self.current_note_type,
            "source_field": self.source_field_combo.currentText(),
            "target_field": self.target_field_combo.currentText(),
            "target_language": self.target_language_combo.currentText(),
            "skip_if_target_filled": self.skip_if_filled_checkbox.isChecked()
        }

class AIFieldTranslatorDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("AI Field Translator")
        self.resize(800, 600)
        
        self.config = load_config()
        self.translation_thread = None
        self.setup_ui()
        self.load_field_mappings()
    
    def setup_ui(self):
        layout = QVBoxLayout(self)
        
        # Create tabs
        self.tabs = QTabWidget()
        self.translation_tab = QWidget()
        self.settings_tab = QWidget()
        
        self.setup_translation_tab()
        self.setup_settings_tab()
        
        self.tabs.addTab(self.translation_tab, "Translation")
        self.tabs.addTab(self.settings_tab, "Settings")
        
        layout.addWidget(self.tabs)
    
    def setup_translation_tab(self):
        layout = QVBoxLayout(self.translation_tab)
        
        # Field mappings
        mappings_group = QGroupBox("Field Mappings")
        mappings_layout = QVBoxLayout()
        
        self.mappings_list = QListWidget()
        self.mappings_list.setAlternatingRowColors(True)
        
        mappings_buttons = QHBoxLayout()
        self.add_mapping_btn = QPushButton("Add Mapping")
        self.remove_mapping_btn = QPushButton("Remove Mapping")
        self.remove_mapping_btn.setEnabled(False)
        
        mappings_buttons.addWidget(self.add_mapping_btn)
        mappings_buttons.addWidget(self.remove_mapping_btn)
        
        mappings_layout.addWidget(self.mappings_list)
        mappings_layout.addLayout(mappings_buttons)
        mappings_group.setLayout(mappings_layout)
        
        # Progress section
        progress_group = QGroupBox("Translation Progress")
        progress_layout = QVBoxLayout()
        
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        
        self.log_output = QTextEdit()
        self.log_output.setReadOnly(True)
        
        progress_layout.addWidget(self.progress_bar)
        progress_layout.addWidget(self.log_output)
        progress_group.setLayout(progress_layout)
        
        # Buttons
        buttons_layout = QHBoxLayout()
        self.start_btn = QPushButton("Start Translation")
        self.stop_btn = QPushButton("Stop")
        self.stop_btn.setEnabled(False)
        self.close_btn = QPushButton("Close")
        
        buttons_layout.addWidget(self.start_btn)
        buttons_layout.addWidget(self.stop_btn)
        buttons_layout.addWidget(self.close_btn)
        
        # Connect signals
        qconnect(self.add_mapping_btn.clicked, self.add_field_mapping)
        qconnect(self.remove_mapping_btn.clicked, self.remove_field_mapping)
        qconnect(self.mappings_list.itemSelectionChanged, self.on_mapping_selection_changed)
        qconnect(self.start_btn.clicked, self.start_translation)
        qconnect(self.stop_btn.clicked, self.stop_translation)
        qconnect(self.close_btn.clicked, self.accept)
        
        layout.addWidget(mappings_group)
        layout.addWidget(progress_group)
        layout.addLayout(buttons_layout)
    
    def setup_settings_tab(self):
        layout = QVBoxLayout(self.settings_tab)
        
        # API settings
        api_group = QGroupBox("API Settings")
        api_layout = QFormLayout()
        
        self.base_url_input = QLineEdit(self.config["api_base_url"])
        self.api_key_input = QLineEdit(self.config["api_key"])
        self.api_key_input.setEchoMode(QLineEdit.EchoMode.Password)
        self.model_input = QLineEdit(self.config["model"])
        
        api_layout.addRow("API Base URL:", self.base_url_input)
        api_layout.addRow("API Key:", self.api_key_input)
        api_layout.addRow("Model:", self.model_input)
        api_group.setLayout(api_layout)

        fallback_group = QGroupBox("OpenAI Fallback")
        fallback_layout = QFormLayout()
        self.openai_fallback_enabled_input = QCheckBox(
            "Use OpenAI automatically if Gemini fails or reaches its quota"
        )
        self.openai_fallback_enabled_input.setChecked(
            self.config.get("openai_fallback_enabled", True)
        )
        self.openai_api_key_input = QLineEdit(self.config.get("openai_api_key", ""))
        self.openai_api_key_input.setEchoMode(QLineEdit.EchoMode.Password)
        self.openai_model_input = QLineEdit(self.config.get("openai_model", "gpt-5-mini"))

        fallback_layout.addRow(self.openai_fallback_enabled_input)
        fallback_layout.addRow("OpenAI API Key:", self.openai_api_key_input)
        fallback_layout.addRow("OpenAI Model:", self.openai_model_input)
        fallback_group.setLayout(fallback_layout)
        
        # Translation settings
        translation_group = QGroupBox("Translation Settings")
        translation_layout = QFormLayout()
        
        self.system_prompt_input = QTextEdit(self.config["system_prompt"])
        self.system_prompt_input.setMaximumHeight(100)
        
        self.max_tokens_input = QSpinBox()
        self.max_tokens_input.setRange(1, 4000)
        self.max_tokens_input.setValue(self.config.get("max_tokens", 1000))
        
        self.temperature_input = QLineEdit(str(self.config.get("temperature", 0.3)))
        
        self.delay_input = QDoubleSpinBox()
        self.delay_input.setRange(0.0, 10.0)
        self.delay_input.setDecimals(1)
        self.delay_input.setSingleStep(0.1)
        self.delay_input.setValue(float(self.config.get("request_delay", 0.5)))
        
        translation_layout.addRow("System Prompt:", self.system_prompt_input)
        translation_layout.addRow("Max Tokens:", self.max_tokens_input)
        translation_layout.addRow("Temperature:", self.temperature_input)
        translation_layout.addRow("Request Delay (seconds):", self.delay_input)
        translation_group.setLayout(translation_layout)
        
        # Save button
        self.save_settings_btn = QPushButton("Save Settings")
        qconnect(self.save_settings_btn.clicked, self.save_settings)
        
        layout.addWidget(api_group)
        layout.addWidget(fallback_group)
        layout.addWidget(translation_group)
        layout.addWidget(self.save_settings_btn)
    
    def add_field_mapping(self):
        dialog = FieldMappingDialog(self)
        if dialog.exec():
            mapping = dialog.get_mapping()
            
            # Check for duplicates
            for i in range(self.mappings_list.count()):
                item_data = self.mappings_list.item(i).data(Qt.ItemDataRole.UserRole)
                if (item_data["note_type"] == mapping["note_type"] and 
                    item_data["source_field"] == mapping["source_field"] and
                    item_data["target_field"] == mapping["target_field"]):
                    QMessageBox.warning(self, "Duplicate Mapping", 
                                      "This mapping already exists.")
                    return
            
            # Add to list
            display_text = f"{mapping['note_type']}: {mapping['source_field']} -> {mapping['target_field']} ({mapping['target_language']})"
            if mapping["skip_if_target_filled"]:
                display_text += " (Skip if filled)"
                
            item = QListWidgetItem(display_text)
            item.setData(Qt.ItemDataRole.UserRole, mapping)
            self.mappings_list.addItem(item)
            
            # Update config
            self.config["field_mappings"].append(mapping)
            save_config(self.config)
    
    def remove_field_mapping(self):
        selected_items = self.mappings_list.selectedItems()
        if not selected_items:
            return
            
        selected_item = selected_items[0]
        mapping = selected_item.data(Qt.ItemDataRole.UserRole)
        
        # Remove from list
        self.mappings_list.takeItem(self.mappings_list.row(selected_item))
        
        # Update config
        field_mappings = []
        for i in range(self.mappings_list.count()):
            field_mappings.append(self.mappings_list.item(i).data(Qt.ItemDataRole.UserRole))
        
        self.config["field_mappings"] = field_mappings
        save_config(self.config)
    
    def on_mapping_selection_changed(self):
        self.remove_mapping_btn.setEnabled(len(self.mappings_list.selectedItems()) > 0)
    
    def load_field_mappings(self):
        self.mappings_list.clear()
        
        for mapping in self.config["field_mappings"]:
            # Handle mappings from older versions that don't have target_language
            target_language = mapping.get("target_language", "English")
            
            display_text = f"{mapping['note_type']}: {mapping['source_field']} -> {mapping['target_field']} ({target_language})"
            if mapping.get("skip_if_target_filled", False):
                display_text += " (Skip if filled)"
                
            item = QListWidgetItem(display_text)
            item.setData(Qt.ItemDataRole.UserRole, mapping)
            self.mappings_list.addItem(item)
    
    def save_settings(self):
        try:
            temperature = float(self.temperature_input.text())
            if not (0 <= temperature <= 1):
                raise ValueError("Temperature must be between 0 and 1")
                
            self.config["api_base_url"] = self.base_url_input.text()
            self.config["api_key"] = self.api_key_input.text()
            self.config["model"] = self.model_input.text()
            self.config["openai_fallback_enabled"] = self.openai_fallback_enabled_input.isChecked()
            self.config["openai_api_key"] = self.openai_api_key_input.text()
            self.config["openai_model"] = self.openai_model_input.text()
            self.config["system_prompt"] = self.system_prompt_input.toPlainText()
            self.config["max_tokens"] = self.max_tokens_input.value()
            self.config["temperature"] = temperature
            self.config["request_delay"] = self.delay_input.value()
            
            save_config(self.config)
            QMessageBox.information(self, "Settings Saved", "Your settings have been saved.")
        except ValueError as e:
            QMessageBox.warning(self, "Invalid Input", str(e))
    
    def start_translation(self):
        has_gemini = bool(self.config.get("api_key", "").strip())
        has_openai_fallback = (
            self.config.get("openai_fallback_enabled", True)
            and bool(self.config.get("openai_api_key", "").strip())
            and bool(self.config.get("openai_model", "").strip())
        )
        if not has_gemini and not has_openai_fallback:
            QMessageBox.warning(
                self,
                "Missing API Key",
                "Enter a Gemini API key or configure the OpenAI fallback in the Settings tab.",
            )
            return
            
        if self.mappings_list.count() == 0:
            QMessageBox.warning(self, "No Mappings", "Please add at least one field mapping.")
            return
        
        # Get all note IDs
        self.log_output.clear()
        self.log_output.append("Finding notes to translate...")
        mappings = []
        
        for i in range(self.mappings_list.count()):
            mapping = self.mappings_list.item(i).data(Qt.ItemDataRole.UserRole)
            mappings.append(mapping)
        
        processed_cache = self.config.setdefault("processed_notes_cache", {})
        note_ids = set()
        stale_cache_removed = False

        mappings_by_note_type = {}
        for mapping in mappings:
            mappings_by_note_type.setdefault(mapping["note_type"], []).append(mapping)

        for note_type, note_type_mappings in mappings_by_note_type.items():
            model_id = get_model_id_by_name(note_type)
            if not model_id:
                continue

            mod_times = get_note_mod_times(model_id)
            for mapping in note_type_mappings:
                cache_key = mapping_cache_key(mapping)
                cache_bucket = processed_cache.setdefault(cache_key, {})

                valid_cache = {}
                pending_count = 0

                for note_id, mod in mod_times.items():
                    cached_mod = cache_bucket.get(str(note_id))
                    if cached_mod is not None and cached_mod >= mod:
                        valid_cache[str(note_id)] = cached_mod
                        continue

                    note_ids.add(note_id)
                    pending_count += 1

                if len(valid_cache) != len(cache_bucket):
                    stale_cache_removed = True
                processed_cache[cache_key] = valid_cache

                self.log_output.append(
                    f"Mapping '{note_type}: {mapping['source_field']} -> {mapping['target_field']}' has {pending_count} notes pending"
                )

        if stale_cache_removed:
            save_config(self.config)

        note_ids = sorted(note_ids)

        if not note_ids:
            QMessageBox.information(self, "No Notes Found", "No new or changed notes need processing.")
            return
        
        # Ask for confirmation
        if not QMessageBox.question(
            self, 
            "Confirm Translation",
            f"This will process {len(note_ids)} notes. Continue?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No
        ) == QMessageBox.StandardButton.Yes:
            return
        
        # Start translation thread
        self.translation_thread = TranslationThread(self.config, note_ids, self)
        self.translation_thread.progress_updated.connect(self.progress_bar.setValue)
        self.translation_thread.log_message.connect(self.log_output.append)
        self.translation_thread.translation_finished.connect(self.on_translation_finished)
        
        self.translation_thread.start()
        self.start_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)
        self.tabs.setTabEnabled(1, False)  # Disable settings tab during translation
    
    def stop_translation(self):
        if self.translation_thread and self.translation_thread.isRunning():
            self.log_output.append("Stopping translation... (This may take a moment)")
            self.translation_thread.stop()
    
    def on_translation_finished(self):
        self.start_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)
        self.tabs.setTabEnabled(1, True)  # Re-enable settings tab
        save_config(self.config)
        self.log_output.append("Translation completed.")
        mw.reset()
    
    def closeEvent(self, event):
        if self.translation_thread and self.translation_thread.isRunning():
            self.translation_thread.stop()
            self.translation_thread.wait()
        event.accept()

# Add-on menu option
def show_translator_dialog():
    dialog = AIFieldTranslatorDialog(mw)
    dialog.exec()

action = QAction("AI Field Translator", mw)
qconnect(action.triggered, show_translator_dialog)
mw.form.menuTools.addAction(action)
