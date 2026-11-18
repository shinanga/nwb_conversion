#!/usr/bin/env python3
"""Preflight validation of limb-placement NWB metadata YAML files.

Usage:
  python validate_nwb_metadata.py --project /mnt/limb-placement-project/Animals
  python validate_nwb_metadata.py --project /mnt/limb-placement-project/Animals --metadata /path/to/file.yml
  python validate_nwb_metadata.py --project /mnt/limb-placement-project/Animals --json report.json

Dependency: pip install pyyaml
"""
from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import datetime
import json
from pathlib import Path
import re
import sys

import yaml

# A blank ticks value means that no reference was supplied: do not invent one.
CONFIG = {
    ("103", "1.25m"): dict(camera_fragment="4_linear_1.25m", scale=0.0011,
                              events={"Reward well 1": "Din20", "Reward well 2": "Din21",
                                      "Pump 1": "Dout29", "Pump 2": "Dout28"}, ticks="Din17"),
    ("103", "2m"): dict(camera_fragment="7_linear_2m", scale=0.0014,
                          events={"Reward well 1": "Din18", "Reward well 2": "Din19",
                                  "Pump 1": "Dout28", "Pump 2": "Dout29"}, ticks=None),
    ("110", "2m"): dict(camera_fragment="3_linear_2m", scale=0.0014,
                          events={"Reward well 1": "Din23", "Reward well 2": "Din24",
                                  "Pump 1": "Dout32", "Pump 2": "Dout31"}, ticks="Din20"),
}
RECORDING_DIR = re.compile(r"^(\d{8})_([a-z0-9]+)_(\d+)_(sleep|run)$", re.I)
METADATA_FILE = re.compile(r"^(\d{8})_([a-z0-9]+)_metadata\.ya?ml$", re.I)


def norm(value):
    return re.sub(r"[^a-z0-9]+", "", str(value).casefold())


def as_list(value):
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def parse_epoch(value):
    try:
        if isinstance(value, bool):
            return None
        number = int(value)
        return number if number > 0 and str(value).strip() in (str(number), f"{number:02d}") else None
    except (ValueError, TypeError):
        return None


def valid_date(date_text):
    try:
        datetime.strptime(date_text, "%Y%m%d")
        return True
    except ValueError:
        return False


class Validator:
    def __init__(self, project):
        self.project = Path(project).expanduser().resolve()
        self.records = []
        self.current = ""

    def add(self, level, message):
        self.records.append({"file": self.current, "level": level, "message": message})

    def error(self, msg): self.add("ERROR", msg)
    def warn(self, msg): self.add("WARNING", msg)
    def ok(self, msg): self.add("PASS", msg)

    def validate(self, yaml_path):
        self.current = str(yaml_path)
        try:
            with yaml_path.open(encoding="utf-8") as stream:
                data = yaml.safe_load(stream)
        except (OSError, yaml.YAMLError) as exc:
            self.error(f"Cannot read YAML: {exc}")
            return
        if not isinstance(data, dict):
            self.error("YAML root must be a mapping.")
            return

        filename_match = METADATA_FILE.fullmatch(yaml_path.name)
        if not filename_match:
            self.error("Metadata filename must be YYYYMMDD_animalid_metadata.yml/.yaml")
            return
        date, animal = filename_match.group(1), filename_match.group(2)
        if not valid_date(date):
            self.error(f"Invalid date in filename: {date}")
            return
        yaml_animal = str((data.get("subject") or {}).get("subject_id", ""))
        if yaml_animal.casefold() != animal.casefold():
            self.error(f"subject.subject_id {yaml_animal!r} differs from filename animal {animal!r}")
        else:
            self.ok("Animal ID matches metadata filename")

        # Accept both <project>/<group>/<animal>/<date> and
        # <project>/<group>/<animal>/raw/<date>, as well as an animal or date
        # directory supplied directly as --project.
        ancestry = [yaml_path.parent, *yaml_path.parents]
        animal_dir = next((p for p in ancestry if p.name.casefold() == animal.casefold()), None)
        if animal_dir is None:
            candidates = [p for p in self.project.rglob("*") if p.is_dir() and p.name.casefold() == animal.casefold()]
            animal_dir = candidates[0] if len(candidates) == 1 else None
        if animal_dir is None:
            self.error(f"Animal directory not uniquely found for {animal} (YAML: {yaml_path}, project: {self.project})")
            return
        raw = animal_dir / "raw" if (animal_dir / "raw").is_dir() else animal_dir
        session_dir = raw / date
        if not session_dir.is_dir():
            self.error(f"Recording date directory missing: {session_dir}")
        recording_days = sorted({p.name for p in raw.iterdir() if p.is_dir() and re.fullmatch(r"\d{8}", p.name) and valid_date(p.name)})
        if date in recording_days:
            expected_id = recording_days.index(date) + 1
            try:
                actual_id = int(data.get("session_id", ""))
            except (ValueError, TypeError):
                actual_id = None
            if actual_id != expected_id:
                self.error(f"session_id={data.get('session_id')!r}; expected {expected_id} (recording date {date}, ordered across {len(recording_days)} dates)")
            else:
                self.ok(f"Session ID {expected_id} matches chronological recording-date index")
        else:
            self.error(f"Date {date} not found among raw recording-day folders")

        folders = {}
        if session_dir.is_dir():
            for child in session_dir.iterdir():
                if not child.is_dir():
                    continue
                match = RECORDING_DIR.fullmatch(child.name)
                if match:
                    folder_date, folder_animal, epoch_text, folder_task = match.groups()
                    epoch = int(epoch_text)
                    if folder_date != date or folder_animal.casefold() != animal.casefold():
                        self.error(f"Recording folder has wrong date/animal: {child.name}")
                    if epoch in folders:
                        self.error(f"Duplicate epoch {epoch} folders: {folders[epoch][0]} and {child}")
                    folders[epoch] = (child, folder_task.capitalize())
                else:
                    self.warn(f"Unrecognized subfolder within date directory: {child.name}")

        tasks = data.get("tasks") or []
        if not isinstance(tasks, list) or not tasks:
            self.error("tasks must be a nonempty list")
            tasks = []
        epoch_to_task = {}
        task_names = set()
        camera_ids = set()
        cameras = data.get("cameras") or []
        if not isinstance(cameras, list):
            self.error("cameras must be a list")
            cameras = []
        for camera in cameras:
            if not isinstance(camera, dict):
                self.error("Invalid cameras entry")
                continue
            cid = parse_epoch(camera.get("id"))
            if cid is None or cid in camera_ids:
                self.error(f"Invalid or duplicate camera id: {camera.get('id')!r}")
            else:
                camera_ids.add(cid)

        for task in tasks:
            if not isinstance(task, dict):
                self.error(f"Invalid task entry: {task!r}")
                continue
            name = task.get("task_name")
            if name not in ("Sleep", "Run"):
                self.error(f"Invalid task_name {name!r}: must be exactly 'Sleep' or 'Run'")
            else:
                task_names.add(name)
            for cid in as_list(task.get("camera_id")):
                if parse_epoch(cid) not in camera_ids:
                    self.error(f"Task {name!r} references undefined camera_id {cid!r}")
            epochs = as_list(task.get("task_epochs"))
            if not epochs:
                self.error(f"Task {name!r} has no task_epochs")
            for raw_epoch in epochs:
                epoch = parse_epoch(raw_epoch)
                if epoch is None:
                    self.error(f"Invalid epoch {raw_epoch!r} for task {name!r}")
                    continue
                if epoch in epoch_to_task:
                    self.error(f"Epoch {epoch} listed multiple times ({epoch_to_task[epoch]!r}, {name!r})")
                else:
                    epoch_to_task[epoch] = name
        if task_names != {"Sleep", "Run"}:
            self.error(f"Expected both 'Sleep' and 'Run' tasks; found {sorted(task_names)}")
        if epoch_to_task:
            expected = set(range(1, max(epoch_to_task) + 1))
            missing = sorted(expected - epoch_to_task.keys())
            if missing:
                self.error(f"Missing task epoch numbers: {missing}")
        for epoch, (folder, folder_task) in sorted(folders.items()):
            declared = epoch_to_task.get(epoch)
            if declared is None:
                self.error(f"Epoch {epoch} exists as recording folder but not in task_epochs: {folder.name}")
            elif declared != folder_task:
                self.error(f"Epoch {epoch} folder task is {folder_task}, YAML says {declared!r}")
        for epoch in sorted(epoch_to_task.keys() - folders.keys()):
            self.error(f"YAML epoch {epoch} has no matching recording folder in {session_dir}")

        # Room may be declared in run task_environment (e.g. 2m linear track_103).
        run_environments = [str(t.get("task_environment", "")) for t in tasks
                            if isinstance(t, dict) and str(t.get("task_name", "")).strip() == "Run"]
        rooms = {room for room in ("103", "110") if any(re.search(rf"(?<!\d){room}(?!\d)", env) for env in run_environments)}
        description = str(data.get("session_description", ""))
        track_text = " ".join([description] + run_environments).casefold()
        if re.search(r"1\s*\.?\s*25\s*m", track_text):
            track = "1.25m"
        elif re.search(r"2\s*m", track_text):
            track = "2m"
        else:
            track = None
        if len(rooms) != 1 or track is None or (next(iter(rooms)), track) not in CONFIG:
            self.error(f"Cannot unambiguously determine supported room/track from Run task_environment={run_environments!r}, session_description={description!r}")
            reference = None
        else:
            room = next(iter(rooms))
            reference = CONFIG[(room, track)]
            self.ok(f"Room {room}, {track} identified")
            if track == "1.25m" and not re.search(r"1\s*\.?\s*25\s*m", description.casefold()):
                self.error("session_description does not identify 1.25m linear track")
            elif track == "2m" and not re.search(r"2\s*m", description.casefold()):
                self.error("session_description does not identify 2m linear track")
        if reference:
            for camera in cameras:
                if not isinstance(camera, dict):
                    continue
                camera_name = str(camera.get("camera_name", ""))
                if norm(reference["camera_fragment"]) not in norm(camera_name):
                    self.error(f"Camera name {camera_name!r} does not contain expected {reference['camera_fragment']!r} (ignoring separators)")
                else:
                    self.ok(f"Camera name {camera_name!r} matches configuration")
                try:
                    scale = float(camera.get("meters_per_pixel"))
                    if abs(scale - reference["scale"]) > 1e-9:
                        self.error(f"Camera {camera_name!r}: meters_per_pixel={scale}, expected {reference['scale']}")
                    else:
                        self.ok(f"Camera {camera_name!r}: meters_per_pixel matches")
                except (ValueError, TypeError):
                    self.error(f"Camera {camera_name!r}: missing or nonnumeric meters_per_pixel")
            event_map = {}
            for event in as_list(data.get("behavioral_events")):
                if not isinstance(event, dict):
                    self.error(f"Invalid behavioral_events entry {event!r}")
                    continue
                event_map[str(event.get("name", "")).strip().casefold()] = str(event.get("description", "")).strip()
            for event_name, expected_code in reference["events"].items():
                actual = event_map.get(event_name.casefold())
                if actual != expected_code:
                    self.error(f"Behavioral event {event_name!r}: {actual!r}; expected {expected_code}")
                else:
                    self.ok(f"Behavioral event {event_name!r}: {expected_code}")
            ticks_expected = reference["ticks"]
            ticks_actual = [evt.get("description") for evt in as_list(data.get("behavioral_events"))
                            if isinstance(evt, dict) and "camera" in str(evt.get("name", "")).casefold()
                            and "tick" in str(evt.get("name", "")).casefold()]
            if ticks_expected and ticks_actual:
                if ticks_expected not in ticks_actual:
                    self.error(f"Run camera ticks expected {ticks_expected}; found {ticks_actual!r}")
                else:
                    self.ok(f"Run camera ticks matches {ticks_expected}")
            elif ticks_expected:
                self.warn(f"Run camera ticks reference is {ticks_expected}, but no explicit 'camera ticks' behavioral event is present")
            else:
                self.warn("Run camera ticks reference is unspecified for this room/track; skipped")

        # Check named associated videos recursively within only this recording day.
        seen_video_entries = set()
        for video in as_list(data.get("associated_video_files")):
            if not isinstance(video, dict):
                self.error(f"Invalid associated_video_files entry: {video!r}")
                continue
            name = str(video.get("name", "")).strip()
            epoch = parse_epoch(video.get("task_epochs"))
            cid = parse_epoch(video.get("camera_id"))
            if not name:
                self.error("Associated video entry lacks a name")
                continue
            if name in seen_video_entries:
                self.error(f"Duplicate video entry: {name}")
            seen_video_entries.add(name)
            if cid not in camera_ids:
                self.error(f"Video {name}: camera_id={video.get('camera_id')!r} is undefined")
            if epoch not in epoch_to_task:
                self.error(f"Video {name}: task_epochs={video.get('task_epochs')!r} is undefined")
            if epoch in folders:
                video_dir = folders[epoch][0]
                candidates = list(video_dir.rglob(name))
                if len(candidates) == 1 and candidates[0].is_file():
                    self.ok(f"Video exists: {candidates[0]}")
                elif not candidates:
                    self.error(f"Video file missing in epoch {epoch} folder: {video_dir / name}")
                else:
                    self.error(f"Ambiguous/invalid video path for {name}: {len(candidates)} matches in {video_dir}")
            else:
                self.error(f"Cannot locate video {name}: epoch {epoch} has no recording folder")
            # Common filename prefix convention; does not infer camera from '.1.mp4'.
            video_match = re.match(r"^(\d{8})_([a-z0-9]+)_(\d+)_(sleep|run)(?:[._-]|$)", name, re.I)
            if not video_match:
                self.warn(f"Video name does not follow recording prefix convention: {name}")
            elif video_match.group(1) != date or video_match.group(2).casefold() != animal.casefold() or int(video_match.group(3)) != epoch:
                self.error(f"Video filename date/animal/epoch disagrees with metadata: {name}")
            elif str(epoch_to_task.get(epoch)).strip() not in ("None", video_match.group(4).capitalize()):
                self.error(f"Video {name}: filename task differs from epoch task")

        # Check associated files (StateScript logs etc.) and epochs.
        for item in as_list(data.get("associated_files")):
            if not isinstance(item, dict):
                self.error(f"Invalid associated_files entry: {item!r}")
                continue
            path_value = str(item.get("path", ""))
            name = str(item.get("name", ""))
            epoch = parse_epoch(item.get("task_epochs"))
            if epoch not in epoch_to_task:
                self.error(f"Associated file {name!r} references unknown task epoch {epoch!r}")
            if path_value:
                filepath = Path(path_value).expanduser()
                if not filepath.is_absolute():
                    filepath = session_dir / filepath
                if filepath.is_file():
                    self.ok(f"Associated file exists: {filepath}")
                else:
                    self.error(f"Associated file missing: {filepath}")
                if name and filepath.name != name:
                    self.error(f"Associated file name differs from path basename: {name!r} vs {filepath.name!r}")
            else:
                self.error(f"Associated file {name!r} has no path")


def find_metadata_files(project):
    return sorted(p for p in project.rglob("*") if p.is_file() and METADATA_FILE.fullmatch(p.name))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", default="/mnt/limb-placement-project/Animals", help="Animals root folder")
    parser.add_argument("--metadata", type=Path, help="Validate one metadata YAML instead of scanning the project")
    parser.add_argument("--json", type=Path, help="Optionally save a machine-readable JSON report")
    args = parser.parse_args()
    validator = Validator(args.project)
    if not validator.project.is_dir():
        print(f"ERROR: Project folder unavailable: {validator.project}", file=sys.stderr)
        return 2
    paths = [args.metadata.expanduser().resolve()] if args.metadata else find_metadata_files(validator.project)
    if not paths:
        print(f"ERROR: No matching metadata YAML files found in {validator.project}", file=sys.stderr)
        return 2
    for path in paths:
        validator.validate(path)
    for record in validator.records:
        print(f"[{record['level']}] {record['file']}: {record['message']}")
    counts = {level: sum(r["level"] == level for r in validator.records) for level in ("ERROR", "WARNING", "PASS")}
    print(f"\nValidated {len(paths)} YAML file(s) | Errors: {counts['ERROR']} | Warnings: {counts['WARNING']} | Passed: {counts['PASS']}")
    if args.json:
        args.json.write_text(json.dumps({"counts": counts, "results": validator.records}, indent=2), encoding="utf-8")
        print(f"JSON report: {args.json}")
    return 1 if counts["ERROR"] else 0


if __name__ == "__main__":
    sys.exit(main())
