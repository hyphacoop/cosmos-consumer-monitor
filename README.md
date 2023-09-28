# cosmos-consumer-monitor

A monitoring tool to issue alerts for aging VSC packets.

![Gif of Mr. Bean checking his watch with a frustrated expression on his face](images/checking_time.gif)

## Features

* Validator set update id collection and checking against each consumer chain unbonding time.
* Validator set update data is recorded to a JSON file.

## Requirements

- python 3.10
- Gaia binary (supported version: v12.0.0)
- Hermes binary (supported version: v1.6.0)

## Installation

1. Install Python dependencies

   ```
   cosmos-consumer-monitor$ python -m venv .env
   cosmos-consumer-monitor$ source .env/bin/activate
   cosmos-consumer-monitor$ python -m pip install -r requirements.txt
   ```

2. Configure the following in `config.toml`:

   * `hermes_config_path`: a Hermes config file will be auto-generated if none is provided
   * Provider chain endpoints
   * Matrix home server URL, room ID, and user/bot token

## How to Use

- Run `python consumer_monitor.py`, or
- modify `cosmos-consumer-monitor.service` and install the service.

> The first time the program is run, it will take several minutes to collect all validator set updates.  
Once a JSON record file is built, subsequent runs will finish much faster.