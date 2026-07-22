#!/usr/bin/env python3

import argparse
import os
import subprocess
import glob
import re
import json
import warnings
import tempfile
import sys

try:
    import boto3
    from botocore.exceptions import ClientError, NoCredentialsError, ProfileNotFound, TokenRetrievalError, SSOTokenLoadError
except ImportError:
    print("\n[ERROR] Required library 'boto3' is not installed.")
    print("Please install it by running:")
    print("    python3 -m pip install boto3")
    sys.exit(1)

# Default hardcoded bucket for standard workflow
DEFAULT_BUCKET = 'ami-carnegie-servicecopies'

def get_args():
    parser = argparse.ArgumentParser(description='Copy SC Video and EM Audio to AWS')

    parser.add_argument(
        '-d', '--directories',
        nargs='+',
        required=True,
        help='One or more bag directories to be processed.'
    )
    parser.add_argument(
        '-c', '--check_only',
        action='store_true',
        help='Check bucket status and metadata without uploading.'
    )
    parser.add_argument(
        '--check_and_upload',
        action='store_true',
        help='Upload ONLY valid bags not already in the bucket.'
    )
    parser.add_argument(
        '-p', '--profile',
        help='AWS profile name for SSO login (ignored if Orange Logic flags are used)',
        default=None
    )
    
    # Mutually exclusive group: can use --orange-test OR --orange-prod, but never both.
    orange_group = parser.add_mutually_exclusive_group()
    orange_group.add_argument(
        '--orange-test',
        action='store_true',
        help='Use Orange Logic TEST bucket and environment credentials'
    )
    orange_group.add_argument(
        '--orange-prod',
        action='store_true',
        help='Use Orange Logic PRODUCTION bucket and environment credentials'
    )

    parser.add_argument(
        '--transcode',
        action='store_true',
        help='Transcode FLAC to MP4 before upload.'
    )
    parser.add_argument(
        '--dry_run',
        action='store_true',
        help='Simulate logic without executing FFmpeg or S3 uploads.'
    )
    
    return parser.parse_args()

def get_s3_client(args):
    """
    Returns a tuple of (s3_client_object, bucket_name).
    Handles switching between SSO, Orange Logic Test, and Orange Logic Prod.
    """
    try:
        if args.orange_test or args.orange_prod:
            # Dynamically look for _TEST or _PROD variables based on the active flag
            suffix = 'TEST' if args.orange_test else 'PROD'
            
            access_key = os.environ.get(f'ORANGE_ACCESS_KEY_{suffix}')
            secret_key = os.environ.get(f'ORANGE_SECRET_KEY_{suffix}')
            region = os.environ.get(f'ORANGE_REGION_{suffix}', 'us-east-1')
            bucket = os.environ.get(f'ORANGE_BUCKET_{suffix}')

            if not all([access_key, secret_key, bucket]):
                print(f"\n[ERROR] Orange Logic {suffix} credentials/bucket not found in environment.")
                print(f"Ensure ORANGE_ACCESS_KEY_{suffix}, ORANGE_SECRET_KEY_{suffix}, and ORANGE_BUCKET_{suffix} are set.")
                sys.exit(1)
            
            session = boto3.Session(
                aws_access_key_id=access_key,
                aws_secret_access_key=secret_key,
                region_name=region
            )
            print(f"--- MODE: Orange Logic {suffix} (Bucket: {bucket}) ---")
            return session.client('s3'), bucket

        else:
            # Standard SSO Logic
            session = boto3.Session(profile_name=args.profile)
            sts = session.client('sts')
            sts.get_caller_identity()
            print(f"--- MODE: Standard SSO (Bucket: {DEFAULT_BUCKET}) ---")
            return session.client('s3'), DEFAULT_BUCKET

    except (NoCredentialsError, ClientError, ProfileNotFound, TokenRetrievalError, SSOTokenLoadError) as e:
        print(f"\n[ERROR] AWS Authentication Failed: {e}")
        if not (args.orange_test or args.orange_prod):
            target_profile = args.profile or 'default'
            print(f"Try running: aws sso login --profile {target_profile}")
        sys.exit(1)

def find_bags(directory):
    try:
        os.listdir(directory)
    except OSError:
        sys.exit(f'Invalid directory: {directory}')

    bags = []
    bag_ids = []
    all_manifests = glob.iglob(os.path.join(directory, '**', 'manifest-md5.txt'), recursive=True)
    
    for filepath in all_manifests:
        if '$RECYCLE' not in filepath:
            bag_path = os.path.split(filepath)[0]
            bags.append(bag_path)
            match = re.findall(r'\d{6}', bag_path)
            bag_ids.append(match[-1] if match else 'UnknownBagID')
    
    return bags, bag_ids

def get_files(source_directory):
    sc_files = {'paths': [], 'names': [], 'media': [], 'json': []}
    em_files = {'paths': [], 'names': [], 'media': [], 'json': []}

    for root, dirs, files in os.walk(source_directory):
        for file in files:
            if file.startswith('._'): continue
            path = os.path.join(root, file)
            lower = file.lower()
            
            if lower.endswith(('sc.mp4', 'sc.json')):
                sc_files['paths'].append(path)
                sc_files['names'].append(file)
                if lower.endswith('.mp4'): sc_files['media'].append(path)
                elif lower.endswith('.json'): sc_files['json'].append(path)
            elif lower.endswith(('em.wav', 'em.flac', 'em.json')):
                em_files['paths'].append(path)
                em_files['names'].append(file)
                if lower.endswith(('.wav', '.flac')): em_files['media'].append(path)
                elif lower.endswith('.json'): em_files['json'].append(path)

    if sc_files['paths']:
        return sc_files['paths'], sc_files['names'], sc_files['media'], sc_files['json']
    elif em_files['paths']:
        return em_files['paths'], em_files['names'], em_files['media'], em_files['json']
    return [], [], [], []

def valid_fn_convention(all_file_list):
    pattern = r'(\w{3}_\d{6}_\w+_(sc|em))'
    invalid = [f for f in all_file_list if not re.search(pattern, f)]
    return invalid if invalid else True

def valid_media_json_match(media_paths, json_paths):
    m_set = {os.path.splitext(os.path.basename(i))[0] for i in media_paths}
    j_set = {os.path.splitext(os.path.basename(i))[0] for i in json_paths}
    return True if m_set == j_set else m_set.symmetric_difference(j_set)

def valid_json_reference(media_files, json_files):
    m_names = {os.path.basename(f) for f in media_files}
    j_names = set()
    for f in json_files:
        with open(f, "r", encoding='utf-8-sig') as jf:
            j_names.add(json.load(jf)['asset']['referenceFilename'])
    return True if m_names == j_names else j_names.symmetric_difference(m_names)

def valid_json_barcode(json_files):
    for f in json_files:
        with open(f, "r", encoding='utf-8-sig') as jf:
            barcode = json.load(jf)['bibliographic']['barcode']
            if not re.search(r'^33433\d+', barcode): return f
    return True

def check_bucket(s3_client, filenames_list, bucket_name):
    if not filenames_list: return []
    common_prefix = os.path.commonprefix(filenames_list)
    found_keys = set()
    try:
        paginator = s3_client.get_paginator('list_objects_v2')
        for page in paginator.paginate(Bucket=bucket_name, Prefix=common_prefix):
            if 'Contents' in page:
                for obj in page['Contents']: found_keys.add(obj['Key'])
    except ClientError: pass

    to_upload = []
    for file in filenames_list:
        found = file in found_keys
        if not found and any(ext in file for ext in ['flac', 'wav']):
            alt_key = file.replace('flac', 'mp4').replace('wav', 'mp4')
            if alt_key in found_keys:
                found = True
        
        if found:
            print(f"  - {file}: Found")
        else:
            print(f"  - {file}: Not Found")
            to_upload.append(file)
            
    return to_upload

def transcode_flac(input_path, output_path, dry_run=False):
    cmd = ["ffmpeg", "-y", "-i", input_path, "-c:a", "aac", "-b:a", "320k", 
           "-dither_method", "rectangular", "-ar", "44100", "-loglevel", "error", output_path]
    if dry_run: return True
    return subprocess.run(cmd).returncode == 0

def create_modified_json(input_path, output_path, new_ref, dry_run=False):
    if dry_run: return True
    try:
        with open(input_path, "r", encoding='utf-8-sig') as f: data = json.load(f)
        data['asset']['referenceFilename'] = new_ref
        with open(output_path, "w", encoding='utf-8') as f: json.dump(data, f, indent=4)
        return True
    except: return False

def prepare_transcodes(file_list, all_bag_files, temp_dir, dry_run=False):
    final, processed_json = [], set()
    json_map = {os.path.splitext(os.path.basename(f))[0]: f for f in all_bag_files if f.lower().endswith('.json')}

    for f in file_list:
        base = os.path.splitext(os.path.basename(f))[0]
        if f.lower().endswith('.flac'):
            mp4_out = os.path.join(temp_dir, base + ".mp4")
            if transcode_flac(f, mp4_out, dry_run):
                final.append(mp4_out)
                if base in json_map:
                    j_out = os.path.join(temp_dir, os.path.basename(json_map[base]))
                    if create_modified_json(json_map[base], j_out, base + ".mp4", dry_run):
                        final.append(j_out)
                        processed_json.add(base)
        elif f.lower().endswith('.json') and base not in processed_json:
            j_out = os.path.join(temp_dir, os.path.basename(f))
            if create_modified_json(f, j_out, base + ".mp4", dry_run): final.append(j_out)
        elif not f.lower().endswith(('.flac', '.json')):
            final.append(f)
    return sorted(list(set(final)))

def cp_files(s3_client, file_list, bucket_name, dry_run=False):
    failures = []
    for f in sorted(file_list):
        if not dry_run and not os.path.exists(f):
             failures.append(f)
             continue
        key = os.path.basename(f)
        if dry_run: print(f"[DRY RUN] Upload: {f} -> s3://{bucket_name}/{key}")
        else:
            try:
                print(f"Uploading: {key}")
                s3_client.upload_file(f, bucket_name, key)
            except Exception as e:
                 print(f"Failed {key}: {e}")
                 failures.append(f)
    return failures

def process_single_directory(directory, arguments, s3_client, bucket_name):
    bags, bag_ids = find_bags(directory)
    summary = {
        'directory': directory, 'num_bags_found': len(bag_ids), 'bag_ids': sorted(bag_ids),
        'em_sc_issue_bags': [], 'invalid_fn_ls': [], 'fn_mismatch_ls': [],
        'json_mismatch_ls': [], 'bc_mismatch_ls': [], 'incomplete_in_bucket': [],
        'failed_uploads': [], 'uploaded_counts': {'mp4': 0, 'wav': 0, 'flac': 0, 'json': 0}
    }

    for bag in sorted(bags):
        bag_id_match = re.findall(r'\d{6}', bag)
        bag_name = bag_id_match[-1] if bag_id_match else os.path.basename(bag)
        if arguments.check_only or arguments.check_and_upload:
            print(f"\nChecking bag: {bag_name}")

        paths, names, media, jsons = get_files(bag)
        fn_map = {os.path.basename(p): p for p in paths}

        if not paths:
            if arguments.check_only or arguments.check_and_upload:
                print(f"  -> WARNING: No EM or SC files found in bag.")
            summary['em_sc_issue_bags'].append(bag)
            continue

        checks = [valid_fn_convention(names), valid_media_json_match(media, jsons),
                  valid_json_reference(media, jsons), valid_json_barcode(jsons)]
        
        if all(c is True for c in checks):
            to_proc = []
            if arguments.check_only or arguments.check_and_upload:
                missing = check_bucket(s3_client, names, bucket_name)
                if missing:
                    summary['incomplete_in_bucket'].append(bag)
                    if arguments.check_and_upload: to_proc = [fn_map[m] for m in missing]
            else:
                to_proc = paths

            if to_proc:
                with tempfile.TemporaryDirectory() as tmp:
                    if arguments.transcode:
                        to_proc = prepare_transcodes(to_proc, paths, tmp, arguments.dry_run)
                    
                    failures = cp_files(s3_client, to_proc, bucket_name, arguments.dry_run)
                    summary['failed_uploads'].extend(failures)
                    for f in to_proc:
                        for ext in ['mp4', 'wav', 'flac', 'json']:
                            if f.lower().endswith(ext): summary['uploaded_counts'][ext] += 1
        else:
            if arguments.check_only or arguments.check_and_upload:
                print("  -> WARNING: Skipping bucket check due to validation errors (e.g. invalid filename, mismatching JSON, etc.).")
            if checks[0] is not True: summary['invalid_fn_ls'].extend(checks[0])
            if checks[1] is not True: summary['fn_mismatch_ls'].append(checks[1])
            if checks[2] is not True: summary['json_mismatch_ls'].append(checks[2])
            if checks[3] is not True: summary['bc_mismatch_ls'].append(checks[3])

    return summary

def print_summary(args, summary):
    print(f"\n=== Summary: {summary['directory']} ===")
    print(f"Bags: {summary['num_bags_found']} | IDs: {summary['bag_ids']}")
    uc = summary['uploaded_counts']
    
    if args.check_only:
        num_needs_upload = len(summary['incomplete_in_bucket'])
        num_complete = summary['num_bags_found'] - num_needs_upload - len(summary['em_sc_issue_bags'])
        print(f"Checks completed: {num_complete} complete, {num_needs_upload} incomplete, {len(summary['em_sc_issue_bags'])} empty/issues.")
    else:
        print(f"{'[DRY RUN] ' if args.dry_run else ''}Uploaded: {uc['mp4']} MP4, {uc['wav']} WAV, {uc['flac']} FLAC, {uc['json']} JSON")
    
    if summary['incomplete_in_bucket']: print(f"Needs Upload: {[os.path.basename(b) for b in summary['incomplete_in_bucket']]}")
    if summary['em_sc_issue_bags']: print(f"Empty/Issue Bags: {[os.path.basename(b) for b in summary['em_sc_issue_bags']]}")
    if summary['invalid_fn_ls']: print(f"Invalid Filenames: {summary['invalid_fn_ls']}")
    if summary['failed_uploads']: print(f"ERRORS: {len(summary['failed_uploads'])} files failed.")

def main():
    args = get_args()
    if args.dry_run: print("\n*** DRY RUN MODE ***\n")
    
    s3_client, active_bucket = get_s3_client(args)

    for directory in args.directories:
        print(f"\nProcessing: {directory}")
        summary = process_single_directory(directory, args, s3_client, active_bucket)
        print_summary(args, summary)

if __name__ == '__main__':
    main()