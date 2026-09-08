"""Seed an isolated, fictional expo dataset. Never points at production."""

import argparse
import os
import sys
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from sqlalchemy import select
from shared.db import (
    Company,
    Posting,
    ResumeProfile,
    PollRun,
    SourceRun,
    Filter,
    get_engine,
    init_db,
    get_session_factory,
    pack_list,
    utcnow,
)
from poller.normalize import make_posting
from poller.store import upsert_companies, upsert_postings

PROFILES = [
    (
        "Software engineering",
        "Backend, infrastructure, and developer tools internships.",
        "Built a Python FastAPI service with PostgreSQL, REST APIs, and pytest regression tests. Deployed scheduled workers using Docker and GitHub Actions CI/CD.\nBuilt a Java TCP server with synchronized operations, background threads, and JUnit tests.\nDeveloped a React and TypeScript application backed by AWS Lambda.\nImplemented a C processor simulator and studied operating systems, data structures, and computer architecture.",
    ),
    (
        "Embedded systems",
        "Embedded firmware, robotics, and real-time systems internships.",
        "Programmed STM32 microcontrollers in C and C++ with FreeRTOS tasks. Integrated sensors over I2C and SPI and diagnosed UART serial communication.\nBuilt a robotics controller with PWM motor control and PID loops. Used oscilloscopes and logic analyzers to debug timing.\nWrote Python hardware test scripts and used Linux and Git for firmware development.",
    ),
    (
        "Chip design",
        "Digital design, verification, and computer architecture internships.",
        "Designed RTL modules in SystemVerilog and Verilog for an FPGA. Wrote testbenches and checked timing with synthesis tools.\nBuilt a pipelined CPU simulator in C, modeling hazards, stalls, virtual memory, and exceptions.\nStudied digital logic, computer architecture, and cache memory. Wrote Python scripts to compare simulation traces.",
    ),
]

JOBS = [
    (
        "Northstar Labs",
        "Backend Engineering Intern",
        "Austin, TX",
        "software_tech",
        "greenhouse",
        "Build Python services with FastAPI and PostgreSQL. Design REST APIs and write pytest regression tests. Collaborate on Docker deployments and CI/CD. Kubernetes experience is preferred.",
    ),
    (
        "Orbit Systems",
        "Developer Infrastructure Intern",
        "Remote, US",
        "software_tech",
        "ashby",
        "Develop internal developer tools and CI/CD automation in Python. Build Linux services, containerize workloads with Docker, and work with AWS. Experience writing automated tests is preferred.",
    ),
    (
        "Atlas Cloud",
        "Software Engineering Intern, Distributed Systems",
        "Seattle, WA",
        "software_tech",
        "lever",
        "Implement Java services and TCP networking. Investigate concurrency, synchronization, and database performance. Write JUnit tests and troubleshoot Linux production systems. Interest in distributed systems is required.",
    ),
    (
        "Forma Studio",
        "Frontend Engineering Intern",
        "San Francisco, CA",
        "software_tech",
        "greenhouse",
        "Build accessible React interfaces with TypeScript. Integrate REST APIs and test responsive layouts. Work with designers on a component library. Interest in browser performance is preferred.",
    ),
    (
        "Lumen Robotics",
        "Embedded Firmware Intern",
        "Austin, TX",
        "robotics",
        "workday",
        "Write C++ firmware for STM32 microcontrollers and FreeRTOS. Integrate I2C and SPI sensors. Debug UART communication with logic analyzers. Work with motor control and real-time scheduling.",
    ),
    (
        "Pico Devices",
        "Firmware Validation Intern",
        "Boston, MA",
        "hardware_general",
        "smartrecruiters",
        "Create Python hardware validation scripts for embedded devices. Debug C firmware and analyze SPI transactions. Use oscilloscopes and Linux test automation. Experience with continuous integration is useful.",
    ),
    (
        "Vector Silicon",
        "RTL Design Intern",
        "Austin, TX",
        "asic_design",
        "ashby",
        "Design RTL in SystemVerilog and Verilog. Build FPGA prototypes and testbenches. Analyze synthesis results and timing closure. Collaborate with engineers developing pipelined processor logic.",
    ),
    (
        "Helix Compute",
        "CPU Architecture Intern",
        "Santa Clara, CA",
        "computer_architecture",
        "greenhouse",
        "Model processor pipelines, caches, and virtual memory in C++. Analyze data hazards, branch prediction, and execution traces. Write Python scripts to compare simulation results. Coursework in computer architecture is required.",
    ),
    (
        "Cedar Networks",
        "Platform Engineering Intern",
        "Remote, US",
        "software_tech",
        "recruitee",
        "Develop Python automation for network services and Linux infrastructure. Write SQL queries, API integrations, and unit tests. Improve container deployment workflows with Docker and Git.",
    ),
    (
        "Aperture Chips",
        "Design Verification Intern",
        "San Jose, CA",
        "asic_design",
        "lever",
        "Develop UVM verification environments in SystemVerilog. Write constrained-random tests, collect coverage, and debug RTL. Python scripting and digital logic knowledge are preferred.",
    ),
    (
        "Meridian Motion",
        "Robotics Controls Intern",
        "Pittsburgh, PA",
        "robotics",
        "workable",
        "Implement C++ control loops and sensor fusion for mobile robots. Tune PID motor controllers and analyze real-time behavior. Experience with ROS 2, Linux, and embedded programming is preferred.",
    ),
    (
        "Flux Energy",
        "Power Electronics Intern",
        "Austin, TX",
        "power_electronics",
        "greenhouse",
        "Design buck converters and power supply PCBs. Measure efficiency and thermal performance using oscilloscopes. Familiarity with MOSFET selection, analog circuits, and electrical lab practices is required.",
    ),
    (
        "Canvas Data",
        "Data Engineering Intern",
        "New York, NY",
        "software_tech",
        "ashby",
        "Build Python and SQL data pipelines. Work with PostgreSQL schemas, scheduled batch processing, and data quality checks. Apache Spark and Airflow are preferred.",
    ),
    (
        "Oak Security",
        "Security Engineering Intern",
        "Remote, US",
        "software_tech",
        "lever",
        "Automate Linux security checks with Python. Investigate network protocols, harden systems, and write regression tests. Familiarity with TCP and container security is useful.",
    ),
    (
        "Keystone Design",
        "Product Design Intern",
        "Chicago, IL",
        "software_tech",
        "simplify",
        "Create user research plans and visual prototypes in Figma. Conduct interviews, document design decisions, and refine typography and visual systems.",
    ),
    (
        "Nimbus Research",
        "Machine Learning Research Intern",
        "Boston, MA",
        "software_tech",
        "greenhouse",
        "Train PyTorch models for computer vision. Design experiments for segmentation and detection. Experience with CUDA, deep learning research, and statistical evaluation is preferred.",
    ),
    (
        "Beacon Labs",
        "Embedded Software Intern",
        "Denver, CO",
        "hardware_general",
        "ashby",
        "Build embedded C drivers for SPI peripherals and develop FreeRTOS applications. Diagnose timing issues and write Python scripts for hardware-in-the-loop tests.",
    ),
    (
        "Harbor Systems",
        "API Engineering Intern",
        "Austin, TX",
        "software_tech",
        "smartrecruiters",
        "Develop REST APIs in C# and .NET. Design SQL schemas, write unit tests, and work with Docker deployments. Familiarity with authentication and background processing is preferred.",
    ),
]


def seed(engine):
    Session = get_session_factory(init_db(engine))
    with Session() as db:
        if db.scalar(select(Posting.id).limit(1)):
            return
        now = utcnow()
        raw = []
        for i, (company, title, loc, sector, source, body) in enumerate(JOBS):
            p = make_posting(
                company_name=company,
                title=title,
                url=f"https://example.com/demo-role/{i + 1}",
                source=source,
                description=body,
                location=loc,
                term="Summer 2027",
            )
            raw.append(p)
        upsert_companies(db, raw)
        rows = upsert_postings(db, raw, alert_eligible=False)
        for i, p in enumerate(rows):
            p.first_seen_at = now - timedelta(hours=i * 5 + 1)
            p.last_seen_at = now - timedelta(minutes=12)
            p.sector_tags = pack_list([JOBS[i][3]])
            p.status = {
                2: "applied",
                5: "interested",
                8: "interested",
                11: "rejected",
            }.get(i, "new")
        for i, c in enumerate(db.scalars(select(Company))):
            c.ats_type = JOBS[i][4]
            c.resolved = True
            c.priority = i in (0, 4, 6)
            c.last_checked_at = now - timedelta(minutes=12)
        for name, prefs, resume in PROFILES:
            db.add(ResumeProfile(name=name, resume_text=resume, preferences=prefs))
        db.add(
            Filter(
                name="Backend & infrastructure",
                sectors=pack_list(["software_tech"]),
                keywords=pack_list(["Python", "API"]),
                channels=pack_list(["email", "ntfy"]),
            )
        )
        for n in range(6):
            tick = now - timedelta(minutes=12 + 30 * n)
            run = PollRun(
                started_at=tick,
                finished_at=tick + timedelta(seconds=42),
                state="complete",
                harvested=180 + n * 3,
                new_postings=3 if n == 0 else 2,
                elapsed=42,
            )
            db.add(run)
            db.flush()
            for source, count in [
                ("greenhouse", 62),
                ("lever", 45),
                ("ashby", 37),
                ("trackers", 36),
            ]:
                db.add(
                    SourceRun(
                        run_id=run.id,
                        source=source,
                        checked_at=tick,
                        count=count,
                        state="ok",
                        elapsed=8,
                    )
                )
        db.commit()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--warm", action="store_true")
    args = parser.parse_args()
    engine = get_engine("sqlite:///demo.db")
    seed(engine)
    if args.warm:
        from shared.matching import rank

        with get_session_factory(engine)() as db:
            jobs = list(db.scalars(select(Posting)))
            for profile in db.scalars(select(ResumeProfile)):
                result, scores, mode = rank(db, jobs, profile)
                print(profile.name, mode, "top:", result[0].title)
    print(
        "Demo ready: DEMO_MODE=1 DATABASE_URL=sqlite:///demo.db .venv/bin/uvicorn backend.main:app --host 0.0.0.0 --port 8000"
    )
