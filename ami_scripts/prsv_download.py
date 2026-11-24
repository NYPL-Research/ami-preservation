#!/usr/bin/env python3
"""
prsv_download.py

An enterprise-grade tool to fetch and download Preservica assets by AMI ID.
Includes: Resumable downloads, Fixity (checksum) verification, and Atomic writes.
"""

import os
import sys
import argparse
import configparser
import logging
import re
import json
import csv
import time
import hashlib
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import List, Tuple, Optional

import requests
from requests.adapters import HTTPAdapter
from requests.exceptions import ChunkedEncodingError, ConnectionError, ReadTimeout
from urllib3.exceptions import ProtocolError
from urllib3.util.retry import Retry
import lxml.etree


# --- Custom Exceptions -------------------------------------------------------

class PreservicaError(Exception):
    """Base exception for all Preservica CLI errors."""

class AuthError(PreservicaError):
    """Raised when authentication fails."""

class ContentError(PreservicaError):
    """Raised when expected content is missing."""


# --- Preservica Client ------------------------------------------------------

class PreservicaClient:
    def __init__(
        self,
        username: str,
        password: str,
        tenant: str,
        base_url: str = "https://nypl.preservica.com",
        pool_maxsize: int = 10,
    ):
        self.base_url = base_url.rstrip('/')
        self.username = username
        self.password = password
        self.tenant = tenant
        self._auth_lock = threading.Lock()

        # Configure session with robust retries for network-level failures
        self.session = requests.Session()
        retry_strategy = Retry(
            total=10,
            backoff_factor=2,
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=["GET", "POST"],
            raise_on_status=False
        )
        adapter = HTTPAdapter(
            max_retries=retry_strategy,
            pool_connections=pool_maxsize,
            pool_maxsize=pool_maxsize,
        )
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)

        self._authenticate()

    def _authenticate(self):
        url = f"{self.base_url}/api/accesstoken/login"
        headers = {"Content-Type": "application/x-www-form-urlencoded"}
        payload = {
            'username': self.username,
            'password': self.password,
            'tenant': self.tenant,
        }
        resp = self.session.post(url, headers=headers, data=payload, timeout=30)
        if not resp.ok:
            logging.error("Auth failed (%s): %s", resp.status_code, resp.text)
            raise AuthError("Failed to obtain access token")
        
        data = resp.json()
        token = data.get("token")
        if not token:
            raise AuthError("Invalid credentials or missing token")
            
        self.session.headers.update({
            "Preservica-Access-Token": token,
            "charset": "UTF-8"
        })
        logging.info("Authenticated successfully")

    def get(self, path: str, **kwargs) -> requests.Response:
        url = path if path.startswith("http") else f"{self.base_url}{path}"
        # Prevent hanging on silent socket drops
        kwargs.setdefault('timeout', (10, 60)) 
        
        resp = self.session.get(url, **kwargs)
        if resp.status_code == 401:
            logging.info("Token expired, re-authenticating")
            with self._auth_lock:
                self._authenticate()
            resp = self.session.get(url, **kwargs)

        # We don't raise_for_status() immediately because we need to handle 206/416 manually
        return resp

    def post(self, path: str, **kwargs) -> requests.Response:
        url = path if path.startswith("http") else f"{self.base_url}{path}"
        kwargs.setdefault('timeout', (10, 60))
        resp = self.session.post(url, **kwargs)
        if resp.status_code == 401:
            with self._auth_lock:
                self._authenticate()
            resp = self.session.post(url, **kwargs)
        resp.raise_for_status()
        return resp

    def search_ami(self, ami_ids: List[str]) -> List[Tuple[Optional[str], Optional[str]]]:
        results = []
        for ami_id in ami_ids:
            if not re.fullmatch(r"\d{6}", ami_id):
                logging.warning("Skipping invalid AMI ID: %s", ami_id)
                results.append((None, None))
                continue

            data = {
                'q': json.dumps({
                    'fields': [
                        {'name': 'specObject.amiId', 'values': [ami_id]},
                        {'name': 'xip.identifier', 'values': ['ioCategory AMIMedia']}
                    ]
                }),
                'start': 0,
                'max': '1',
                'metadata': ['xip.title', 'id']
            }
            try:
                resp = self.post("/api/content/search?", data=data)
                json_body = resp.json()
                md = json_body['value']['metadata'][0]
                title = next(item['value'] for item in md if item['name']=="xip.title")
                io_uuid = next(item['value'] for item in md if item['name']=="id")
                results.append((title, io_uuid))
                logging.info("Found AMI %s → IO UUID %s", title, io_uuid)
            except Exception:
                logging.warning("No result for AMI ID %s", ami_id)
                results.append((None, None))
        return results

    def get_content_object_uuids(self, io_uuid: str, specifier: str) -> List[str]:
        headers = {"Accept": "application/xml"}
        def fetch(spec: str):
            path = f"/api/entity/information-objects/{io_uuid}/representations/{spec}"
            resp = self.get(path, headers=headers)
            if resp.status_code == 404:
                return None
            resp.raise_for_status()
            return resp

        resp = fetch(specifier)
        if resp is None and specifier == "preservation_2":
            resp = fetch("preservation_1")

        if resp is None:
            raise ContentError(f"No representation for {io_uuid}")

        tree = lxml.etree.fromstring(resp.content)
        texts = tree.xpath("//*[local-name()='ContentObject']/text()")
        
        uuid_re = re.compile(r'[0-9a-fA-F]{8}(-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}')
        uuids = [uuid_re.search(t).group(0).lower() for t in texts if uuid_re.search(t)]
        
        return list(dict.fromkeys(uuids))

    def _get_fixity(self, xml_tree) -> Tuple[Optional[str], Optional[str]]:
        """Extract hash value and algorithm from Preservica XML."""
        fixity = xml_tree.xpath("//*[local-name()='Fixity']")
        if fixity:
            val = fixity[0].xpath(".//*[local-name()='FixityValue']/text()")
            alg = fixity[0].xpath(".//*[local-name()='FixityAlgorithm']/text()")
            if val and alg:
                return val[0].strip(), alg[0].strip().lower()
        return None, None

    def download_bitstream(self, co_uuid: str, output_dir: str) -> bool:
        os.makedirs(output_dir, exist_ok=True)
        base_path = f"/api/entity/content-objects/{co_uuid}/generations/latest-active/bitstreams/1"
        content_path = f"{base_path}/content"

        # 1. Get Metadata for filename, size, and hash
        meta_resp = self.get(base_path, headers={"Accept": "application/xml"})
        meta_resp.raise_for_status()
        tree = lxml.etree.fromstring(meta_resp.content)
        
        filename_nodes = tree.xpath("//*[local-name()='Filename']/text()")
        filename = filename_nodes[0] if filename_nodes else f"{co_uuid}.bin"
        filepath = os.path.join(output_dir, filename)
        part_path = filepath + ".part"

        size_nodes = tree.xpath("//*[local-name()='FileSize']/text() | //*[local-name()='Size']/text()")
        total_size = int(size_nodes[0].strip()) if size_nodes else None
        expected_hash, hash_algo = self._get_fixity(tree)

        # 2. Resumable Download Loop
        retries = 0
        max_retries = 30
        last_logged_at = 0.0

        while True:
            current_pos = os.path.getsize(part_path) if os.path.exists(part_path) else 0

            # Exit if the part file is already complete
            if total_size and current_pos >= total_size:
                break

            headers = {'Range': f'bytes={current_pos}-'} if current_pos > 0 else {}
            
            try:
                # Using 'with' ensures the connection is closed properly
                with self.get(content_path, headers=headers, stream=True) as resp:
                    if resp.status_code == 416: # Range Not Satisfiable
                        break

                    if resp.status_code not in (200, 206):
                        retries += 1
                        if retries > max_retries:
                            logging.error(
                                "Failed after %d retries for %s (HTTP %d)",
                                max_retries, filename, resp.status_code
                            )
                            return False
                        time.sleep(min(retries * 5, 60))
                        logging.warning(
                            "Unexpected HTTP %d for %s. Retrying %d/%d...",
                            resp.status_code, filename, retries, max_retries
                        )
                        continue

                    # If 206 Partial Content, we append. If 200, we start over.
                    mode = 'ab' if resp.status_code == 206 else 'wb'
                    if mode == 'wb':
                        current_pos = 0

                    with open(part_path, mode) as f:
                        for chunk in resp.iter_content(chunk_size=1024*1024): # 1MB chunks
                            if chunk:
                                f.write(chunk)
                                current_pos += len(chunk)
                                retries = 0 # Successful data received; reset retry counter

                                # Throttle progress logging so concurrent downloads
                                # don't flood the log; one line per file per ~5s.
                                now = time.monotonic()
                                if now - last_logged_at >= 5:
                                    last_logged_at = now
                                    if total_size:
                                        pct = (current_pos / total_size) * 100
                                        logging.info("%s: %.2f%%", filename, pct)
                                    else:
                                        logging.info("%s: %d bytes", filename, current_pos)

                # Re-check size after chunking finishes
                if total_size and current_pos >= total_size:
                    break

            except (ChunkedEncodingError, ConnectionError, ProtocolError, ReadTimeout) as e:
                retries += 1
                if retries > max_retries:
                    logging.error("Failed after %d retries for %s", max_retries, filename)
                    return False
                time.sleep(min(retries * 5, 60))
                logging.warning("Interrupted (%s). Retrying %d/%d...", e, retries, max_retries)

        # 3. Fixity Verification and Atomic Rename
        if expected_hash and hash_algo in ['md5', 'sha1', 'sha256']:
            logging.info(f"Verifying {hash_algo.upper()} for {filename}...")
            hasher = hashlib.new(hash_algo)
            with open(part_path, "rb") as f:
                for chunk in iter(lambda: f.read(1024*1024), b""):
                    hasher.update(chunk)
            
            actual_hash = hasher.hexdigest().lower()
            if actual_hash != expected_hash.lower():
                logging.error(f"Fixity mismatch for {filename}! Expected: {expected_hash}, Got: {actual_hash}")
                logging.warning(f"Deleting corrupted file: {part_path}")
                os.remove(part_path)
                return False

        # Final move from .part to real filename
        os.rename(part_path, filepath)
        logging.info(f"Download complete: {filename}")
        return True


# --- CLI / Main -------------------------------------------------------------

def load_credentials(config_path: Optional[str]):
    username = os.getenv("PRESERVICA_USERNAME")
    password = os.getenv("PRESERVICA_PASSWORD")
    tenant   = os.getenv("PRESERVICA_TENANT")

    if username and password and tenant:
        return username, password, tenant

    config_path = config_path or os.path.expanduser("~/.preservica_credentials.ini")
    cfg = configparser.ConfigParser()
    if not cfg.read(config_path):
        logging.error("Cannot read config file %s", config_path)
        sys.exit(1)
    creds = cfg["default"]
    return creds["username"], creds["password"], creds["tenant"]

def read_ids_from_csv(csv_path: str) -> List[str]:
    ids = []
    pattern = re.compile(r"^\d{6}$")
    with open(csv_path, newline='') as fp:
        reader = csv.reader(fp)
        for row in reader:
            if row and pattern.fullmatch(row[0].strip()):
                ids.append(row[0].strip())
    return ids

def parse_args():
    parser = argparse.ArgumentParser(description="Fetch and download Preservica assets")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("-i", "--ids", nargs="+")
    group.add_argument("-c", "--csv")
    parser.add_argument("--config-file", default=None)
    parser.add_argument("-r", "--representation", choices=["access", "production", "preservation"], default="production")
    parser.add_argument("-o", "--output", default=".")
    parser.add_argument("-w", "--workers", type=int, default=1,
                         help="Number of files to download concurrently (default: 1)")
    parser.add_argument("-v", "--verbose", action="store_true")
    return parser.parse_args()

def main():
    args = parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s: %(message)s"
    )

    ami_ids = read_ids_from_csv(args.csv) if args.csv else args.ids
    user, pw, tenant = load_credentials(args.config_file)
    client = PreservicaClient(user, pw, tenant, pool_maxsize=max(10, args.workers * 2))

    rep_map = {"access": "access_1", "production": "preservation_2", "preservation": "preservation_1"}
    results = client.search_ami(ami_ids)

    # Resolve every AMI ID to its content-object UUIDs first (cheap metadata
    # calls), then fan the actual bitstream downloads out across a thread pool.
    co_uuids: List[str] = []
    for title, io_uuid in results:
        if not (title and io_uuid): continue
        spec = rep_map[args.representation]
        try:
            co_uuids.extend(client.get_content_object_uuids(io_uuid, spec))
        except PreservicaError as e:
            logging.error("Error processing %s: %s", io_uuid, e)

    succeeded = failed = 0
    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = {
            executor.submit(client.download_bitstream, co, args.output): co
            for co in co_uuids
        }
        for future in as_completed(futures):
            co = futures[future]
            try:
                ok = future.result()
            except Exception as e:
                logging.error("Error downloading content object %s: %s", co, e)
                ok = False
            if ok:
                succeeded += 1
            else:
                failed += 1

    logging.info("Downloaded %d/%d files successfully", succeeded, succeeded + failed)
    if failed:
        sys.exit(1)

if __name__ == "__main__":
    main()
