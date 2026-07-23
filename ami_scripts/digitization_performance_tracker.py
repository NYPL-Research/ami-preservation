#!/usr/bin/env python3

import argparse
import jaydebeapi
import os
import pandas as pd
import seaborn as sns
import matplotlib.pyplot as plt
import numpy as np
import datetime
from matplotlib.backends.backend_pdf import PdfPages

# Setup display options for better readability in the output
pd.set_option('display.max_rows', None)
pd.set_option('display.max_columns', None)
pd.set_option('display.width', None)
pd.set_option('display.max_colwidth', None)

def get_args():
    parser = argparse.ArgumentParser(description='Generate Production Stats & Visualizations from AMIDB')
    parser.add_argument('-f', '--fiscal', action='store_true',
                        help='organize stats and visualizations by fiscal year instead of calendar year')
    parser.add_argument('-e', '--engineer', nargs='+',
                        help='Filter output by specific engineers (last names).')
    parser.add_argument('-H', '--historical', action='store_true',
                        help='Analyze data from all years instead of just the current year.')
    parser.add_argument('-p', '--previous-fiscal', nargs='?', const='auto', default=None,
                        help='Analyze data from the *previous* fiscal year (when used alone), '
                             'or from a specified fiscal year (e.g. -p FY23, -p FY24).')
    parser.add_argument('--vendor', action='store_true',
                        help='Pull data from vendor table only.')
    parser.add_argument('-c', '--combined', action='store_true',
                        help='Pull data from both vendor and in-house.')
    parser.add_argument('--input_csv',
                        help='Path to a CSV file to use as input instead of fetching from the database.',
                        type=str, default=None)
    parser.add_argument('--output_csv',
                        help='Path to output the fetched database data as CSV.',
                        type=str, default=None)
    return parser.parse_args()


def fetch_data_from_jdbc(args):
    # load environment
    server_ip = os.getenv('FM_SERVER')
    database_name = os.getenv('AMI_DATABASE')
    username = os.getenv('AMI_DATABASE_USERNAME')
    password = os.getenv('AMI_DATABASE_PASSWORD')
    jdbc_path = os.path.expanduser('~/Desktop/ami-preservation/ami_scripts/jdbc/fmjdbc.jar')

    conn = None
    df = pd.DataFrame()

    try:
        conn = jaydebeapi.connect(
            'com.filemaker.jdbc.Driver',
            f'jdbc:filemaker://{server_ip}/{database_name}',
            [username, password],
            jdbc_path
        )
        print("Connection to AMIDB successful!")
        print("Now Fetching Data (Expect 2-3 minutes)")

        # -- If we want to optionally fetch vendor data, define it here:
        vendor_query = '''
        SELECT
            "asset.referenceFilename",
            "bibliographic.primaryID",
            "technical.dateCreated",
            "technical.fileFormat",
            "technical.fileSize.measure",
            "technical.durationMilli.measure",
            "asset.fileRole",
            "mediaType",
            "bibliographic.vernacularDivisionCode",
            "source.object.format",
            "source.object.type",
            "cmsCollectionTitle"
        FROM tbl_vendor_mediainfo
        '''

        inhouse_query = '''
        SELECT
            "asset.referenceFilename",
            "bibliographic.primaryID",
            "technical.dateCreated",
            "technical.fileFormat",
            "technical.fileSize.measure",
            "technical.durationMilli.measure",
            "asset.fileRole",
            "digitizer.operator.lastName",
            "digitizationProcess.playbackDevice.model", 
            "digitizationProcess.playbackDevice.serialNumber",
            "bibliographic.vernacularDivisionCode",
            "source.object.format",
            "source.object.type",
            "cmsCollectionTitle",
            "projectType"
        FROM tbl_metadata
        '''

        curs = conn.cursor()

        # If user wants vendor data only or combined
        if args.vendor or args.combined:
            # fetch vendor data
            curs.execute(vendor_query)
            columns = [desc[0] for desc in curs.description]
            vendor_data = [dict(zip(columns, row)) for row in curs.fetchall()]
            df_vendor = pd.DataFrame(vendor_data)
            # Force these columns for vendor data
            df_vendor['source'] = 'Vendor'
            df_vendor['projectType'] = 'Programmatic Digitization'
            
            # **Add or fill** digitizer.operator.lastName => 'Vendor'
            if 'digitizer.operator.lastName' not in df_vendor.columns:
                df_vendor['digitizer.operator.lastName'] = 'Vendor'
            else:
                df_vendor['digitizer.operator.lastName'] = df_vendor['digitizer.operator.lastName'].fillna('Vendor')
            
            if args.combined:
                # also fetch in-house
                curs.execute(inhouse_query)
                columns = [desc[0] for desc in curs.description]
                inhouse_data = [dict(zip(columns, row)) for row in curs.fetchall()]
                df_inhouse = pd.DataFrame(inhouse_data)
                df_inhouse['source'] = 'In-House'
                df = pd.concat([df_vendor, df_inhouse], ignore_index=True)
            else:
                # vendor only
                df = df_vendor
        else:
            # default: in-house only
            curs.execute(inhouse_query)
            columns = [desc[0] for desc in curs.description]
            data = [dict(zip(columns, row)) for row in curs.fetchall()]
            df = pd.DataFrame(data)
            df['source'] = 'In-House'

        print(f"Data fetched successfully!")
        print(f"Total records fetched: {len(df)}")

    except Exception as e:
        print(f"Failed to connect or execute query: {e}")

    finally:
        if conn:
            conn.close()

    return df


def format_file_size(total_bytes):
    units = ["Bytes", "KB", "MB", "GB", "TB", "PB"]
    size = total_bytes
    unit_index = 0

    while size >= 1024 and unit_index < len(units) - 1:
        size /= 1024.0
        unit_index += 1

    if unit_index == 0:
        return f"{size:.0f} {units[unit_index]}"  # No decimal places for bytes
    else:
        return f"{size:.2f} {units[unit_index]}"  # Two decimal places for all other units


def get_fiscal_year(date):
    year = date.year
    month = date.month

    if month >= 7:
        fiscal_year = year + 1
    else:
        fiscal_year = year

    return f"FY{str(fiscal_year)[2:]}"


def process_data(df, args, fiscal=False):
    def convert_date(date_str):
        if "-" in str(date_str):
            # YYYY-MM-DD
            return pd.to_datetime(date_str, format='%Y-%m-%d', errors='coerce')
        else:
            # M/D/Y
            return pd.to_datetime(date_str, format='%m/%d/%Y', errors='coerce')

    df['technical.dateCreated'] = df['technical.dateCreated'].apply(convert_date)

    # Filter by engineer if specified
    if args.engineer:
        df = df[df['digitizer.operator.lastName'].isin(args.engineer)].copy()

    # Assign calendar/fiscal info
    df.loc[:, 'calendar_year'] = df['technical.dateCreated'].dt.year
    df.loc[:, 'fiscal_year'] = df['technical.dateCreated'].apply(get_fiscal_year)
    df.loc[:, 'month'] = df['technical.dateCreated'].dt.strftime('%Y-%m')

    current_date = datetime.datetime.now()
    current_fiscal_year = get_fiscal_year(current_date)
    prior_fiscal_year = f"FY{int(current_fiscal_year[2:]) - 1}"

    # 1) If user passed -H or --historical, skip year filtering
    if args.historical:
        return df

    # 2) If user specified -p with or without a year
    if args.previous_fiscal is not None:
        if args.previous_fiscal == 'auto':
            # They just typed -p (no argument)
            df = df[df['fiscal_year'] == prior_fiscal_year].copy()
        else:
            # They typed something like -p FY20 or -p 2020
            chosen_val = args.previous_fiscal
            if chosen_val.upper().startswith("FY"):
                # e.g. "FY24", "FY20"
                df = df[df['fiscal_year'] == chosen_val.upper()].copy()
            else:
                # Interpret as a calendar year integer
                # e.g. "2020" => 2020
                try:
                    cal_year = int(chosen_val)
                    df = df[df['calendar_year'] == cal_year].copy()
                except ValueError:
                    print(f"Warning: '{chosen_val}' is not a valid FY or year. No filtering applied.")
                    # or you could exit, or revert to an unfiltered df
                    # for now let's revert to unfiltered or skip
                    pass
        return df

    # 3) If user explicitly wants the *current* fiscal year
    if fiscal:
        df = df[df['fiscal_year'] == current_fiscal_year].copy()
        return df

    # 4) Otherwise, default to the current calendar year
    df = df[df['calendar_year'] == current_date.year].copy()
    return df


def classify_media_types(df):
    # Create a new DataFrame to avoid modifying the original while it's being sliced
    df_copy = df.copy()

    # Initialize an empty list to store the media types
    media_type = []

    # Iterate over each row in the 'source.object.type' column to classify media types
    for row in df_copy['source.object.type']:
        if row in ['video cassette analog', 'video cassette digital', 'video optical disc', 'video reel']:
            media_type.append('video')
        elif row == 'film':
            media_type.append('film')
        elif row == 'data optical disc':
            media_type.append('data')
        else:
            media_type.append('audio')

    # Assign the list of media types to the 'media_type' column using .loc to ensure proper handling
    df_copy.loc[:, 'media_type'] = media_type

    # Return the modified DataFrame
    return df_copy

def display_monthly_output(df, args, fiscal=False):
    
    df_filtered = df.copy()
    df_filtered['technical.fileSize.measure'] = pd.to_numeric(df_filtered['technical.fileSize.measure'], errors='coerce')

    total_file_size = df_filtered['technical.fileSize.measure'].sum()
    formatted_file_size = format_file_size(total_file_size)
    
    df_pm = df[df['asset.fileRole'] == 'pm']
        # Ensure 'digitizer.operator.lastName' column exists, even if vendor data doesn't have it
    if 'digitizer.operator.lastName' not in df_pm.columns:
        df_pm['digitizer.operator.lastName'] = 'Vendor'
    else:
        # If the column exists (e.g. combined data), fill any missing rows with 'Vendor'
        df_pm.loc[:, 'digitizer.operator.lastName'] = df_pm['digitizer.operator.lastName'].fillna('Vendor')

    df_pm = classify_media_types(df_pm) 

    current_date = datetime.datetime.now()
    current_fiscal_year = get_fiscal_year(current_date)
    prior_fiscal_year = f"FY{int(current_fiscal_year[2:]) - 1}"

    if args.historical:
        year_label = "All Years"
    elif args.previous_fiscal == 'auto':
        year_label = prior_fiscal_year
    elif args.previous_fiscal:  # e.g. "FY24"
        year_label = args.previous_fiscal
    elif fiscal:
        year_label = current_fiscal_year
    else:
        year_label = str(current_date.year)

    combine_dict = {
        'MUS': 'MUS + RHA',
        'RHA': 'MUS + RHA',
        'mym': 'MUS + RHA',
        'myh': 'MUS + RHA',
        'SCM': 'SCH',
        'SCL': 'SCH',
        'scb': 'SCH',
        'scd': 'SCH',
        'THE': 'THE + TOFT',
        'TOFT': 'THE + TOFT',
        'myt': 'THE + TOFT',
        'DAN': 'DAN',
        'myd': 'DAN',
        'mao': 'MSS',
        'MSS': 'MSS'
    }

    total_counts_by_project_type = (
        df_pm.groupby('projectType')['bibliographic.primaryID']
        .nunique()
        .reset_index()
        .rename(columns={'bibliographic.primaryID': 'Unique Items'})
    )

    # Group by media type, month, and division code, then count unique IDs
    monthly_media_counts = df_pm.groupby(['media_type', 'month', 'bibliographic.vernacularDivisionCode']).agg({
        'bibliographic.primaryID': 'nunique'
    }).reset_index()

    # Rename columns for clarity
    monthly_media_counts.rename(columns={'bibliographic.primaryID': 'Unique Items'}, inplace=True)

    # Calculate the total per media type to use for percentage calculations
    total_media_counts = monthly_media_counts.groupby('media_type').agg({
        'Unique Items': 'sum'
    }).rename(columns={'Unique Items': 'Total Items Per Media'}).reset_index()

    # Use total_media_counts for calculations
    print(f"Total unique items per media type across {year_label}:")
    print(total_media_counts)

    total_unique_items = total_media_counts['Total Items Per Media'].sum()
    print(f"Total of all media types: {total_unique_items}")

    # Merge the totals back to get percentages for display purposes and then drop unnecessary columns
    total_media_counts_by_division = monthly_media_counts.groupby(['media_type', 'bibliographic.vernacularDivisionCode']).agg({
        'Unique Items': 'sum'
    }).reset_index().merge(total_media_counts, on='media_type')

    # Apply division combinations
    total_media_counts_by_division['bibliographic.vernacularDivisionCode'] = total_media_counts_by_division['bibliographic.vernacularDivisionCode'].replace(combine_dict)

    # Re-aggregate after combining divisions
    total_media_counts_by_division = total_media_counts_by_division.groupby(['media_type', 'bibliographic.vernacularDivisionCode']).agg({
        'Unique Items': 'sum'
    }).reset_index().merge(total_media_counts, on='media_type')

    total_media_counts_by_division['Percentage'] = (total_media_counts_by_division['Unique Items'] / total_media_counts_by_division['Total Items Per Media']) * 100
    total_media_counts_by_division['Percentage'] = total_media_counts_by_division['Percentage'].apply(lambda x: f"{x:.2f}%")

    print(total_media_counts_by_division)

    # Define the minimum percentage threshold
    min_percentage = 5.0

    # Aggregate small slices into "Others" to maintain accurate representation
    total_media_counts_by_division['Percentage_float'] = total_media_counts_by_division['Percentage'].str.rstrip('%').astype(float)
    
    above_thresh = total_media_counts_by_division[total_media_counts_by_division['Percentage_float'] >= min_percentage].copy()
    below_thresh = total_media_counts_by_division[total_media_counts_by_division['Percentage_float'] < min_percentage].copy()
    
    if not below_thresh.empty:
        others_aggregated = below_thresh.groupby('media_type').agg({
            'Unique Items': 'sum',
            'Total Items Per Media': 'first'
        }).reset_index()
        others_aggregated['bibliographic.vernacularDivisionCode'] = 'Others'
        
        others_aggregated['Percentage'] = (others_aggregated['Unique Items'] / others_aggregated['Total Items Per Media']) * 100
        others_aggregated['Percentage'] = others_aggregated['Percentage'].apply(lambda x: f"{x:.2f}%")
        
        total_media_counts_by_division = pd.concat([above_thresh, others_aggregated], ignore_index=True)
    else:
        total_media_counts_by_division = above_thresh

    total_media_counts_by_division = total_media_counts_by_division.drop(columns=['Percentage_float'])

    # Adjust regex to capture only up to the first significant identifier (up to version number)
    df_pm['core_id'] = df_pm['asset.referenceFilename'].str.extract(r'(^.+?)_v\d+')[0]
    df_pm['is_multitrack'] = df_pm['asset.referenceFilename'].str.contains(r's\d+_pm')

    # Define the custom aggregation function
    def custom_aggregate(group, include_groups=False):
        if group['is_multitrack'].any():
            # Only consider the duration of the first stream if it's a multi-track
            return group.loc[group['is_multitrack'], 'technical.durationMilli.measure'].iloc[0]
        else:
            # For non-multi-track, sum the durations in the group
            return group['technical.durationMilli.measure'].sum()

    # Adjust the groupby and apply the function with include_groups set to False
    monthly_unique_durations = df_pm.groupby(['month', 'core_id']).apply(custom_aggregate, include_groups=False).reset_index()

    # Rename the resulting series manually after resetting the index
    monthly_unique_durations.rename(columns={0: 'Total Duration (ms)'}, inplace=True)

    # Sum these durations for each month
    monthly_durations = monthly_unique_durations.groupby('month').agg({'Total Duration (ms)': 'sum'}).reset_index()

    # Calculate the grand total of durations
    grand_total_duration = monthly_durations['Total Duration (ms)'].sum()

    # Convert the grand total duration from milliseconds to HH:MM:SS
    hours, remainder = divmod(grand_total_duration / 1000, 3600)
    minutes, seconds = divmod(remainder, 60)

    # Formatted string for the grand total duration
    formatted_grand_total_duration = f"{int(hours):02}:{int(minutes):02}:{int(seconds):02}"

    print(f"Total duration of all digitized items (HH:MM:SS): {formatted_grand_total_duration}")

    # --- compute total bytes per file role ---
    data_by_role = (
        df_filtered
        .groupby('asset.fileRole')['technical.fileSize.measure']
        .sum()
        .reset_index()
        .rename(columns={'technical.fileSize.measure': 'totalBytes'})
    )
    
    # --- print human-readable breakdown by role ---
    print("\nTotal Data by Role:")
    for _, row in data_by_role.iterrows():
        role = row['asset.fileRole']
        size = format_file_size(row['totalBytes'])
        print(f"  {role}: {size}")
    
    # compression ratio for EM → AAC
    compression_ratio = 320_000 / 684_000

    # get raw bytes for SC and EM
    sc_bytes = data_by_role.loc[data_by_role['asset.fileRole']=='sc', 'totalBytes'].sum()
    em_bytes = data_by_role.loc[data_by_role['asset.fileRole']=='em', 'totalBytes'].sum()

    # estimate EM size after AAC compression
    estimated_em = em_bytes * compression_ratio

    # total DAMS footprint = SC + compressed EM
    estimated_dams = sc_bytes + estimated_em

    # print it out
    print("\nEstimated DAMS Data:")
    print(f"  sc: {format_file_size(sc_bytes)}")
    print(f"  em (compressed): {format_file_size(estimated_em)}")
    print(f"  TOTAL → {format_file_size(estimated_dams)}")
    
    # 1) Check if the columns exist in df_pm
    has_equipment_cols = (
        'digitizationProcess.playbackDevice.model' in df_pm.columns and
        'digitizationProcess.playbackDevice.serialNumber' in df_pm.columns
    )

    if has_equipment_cols:
        # 2) Perform the grouping if columns exist
        equipment_usage = (
            df_pm.groupby(['digitizationProcess.playbackDevice.model', 'digitizationProcess.playbackDevice.serialNumber'])
                ['bibliographic.primaryID']
                .nunique()
                .reset_index()
        )
        equipment_usage.columns = ['Device Model', 'Serial Number', 'Unique Items']
        equipment_usage['Label'] = equipment_usage['Device Model'] + " (" + equipment_usage['Serial Number'] + ")"
        equipment_usage = equipment_usage.sort_values(by='Unique Items', ascending=False)
        print("\nEquipment usage has been successfully grouped!")
    else:
        # 3) Otherwise, skip or create an empty DataFrame
        print("\nSkipping Equipment usage grouping (not available in vendor-only data).")
        equipment_usage = pd.DataFrame(columns=['Device Model', 'Serial Number', 'Unique Items', 'Label'])

    # Calculate unique items per SPEC Collection ID
    spec_collection_usage = df_pm.groupby('cmsCollectionTitle')['bibliographic.primaryID'].nunique().reset_index()
    spec_collection_usage.columns = ['SPEC Collection Title', 'Unique Items']
    spec_collection_usage = spec_collection_usage.sort_values(by='Unique Items', ascending=False)

    # Grouping data by operator and month, and aggregating unique IDs and average duration
    output_by_operator = df_pm.groupby(['digitizer.operator.lastName', 'month']).agg({
        'bibliographic.primaryID': 'nunique',
        'technical.durationMilli.measure': 'mean'  # Calculate average duration
    }).reset_index()

    total_pm_summed = df_pm['bibliographic.primaryID'].nunique()
    print(f"Total digitized items (unique pm across all years): {total_pm_summed}")

    output_by_operator['month'] = pd.to_datetime(output_by_operator['month'])
    output_by_operator = output_by_operator.sort_values('month')

    output_sum = output_by_operator.groupby('digitizer.operator.lastName').agg({
        'bibliographic.primaryID': 'sum',
        'technical.durationMilli.measure': 'mean'
    }).reset_index()
    output_sum['month'] = 'Total'
    output_by_operator_summed = pd.concat([output_by_operator, output_sum], ignore_index=True)

    output_by_operator_summed['formatted_avg_duration'] = pd.to_timedelta(output_by_operator_summed['technical.durationMilli.measure'], unit='ms').dt.components.apply(
        lambda x: f"{int(x['hours']):02}:{int(x['minutes']):02}:{int(x['seconds']):02}", axis=1)

    total_items_per_month_summed = output_sum['bibliographic.primaryID'].sum()
    print(f"Total digitized items (summed across months): {total_items_per_month_summed}")

    print(f'\nTotal file size from all records: {formatted_file_size}')
    print(output_by_operator_summed)

    # Decide what "hue" to use based on vendor/combined
    if args.combined or args.vendor:
        # We want lines by source
        hue_column = 'source'
        legend_title = 'Source'
    else:
        # In-house only => lines by operator
        hue_column = 'digitizer.operator.lastName'
        legend_title = 'Digitizer'

    if hue_column == 'source':
        # group by month + source
        output_line = (
            df_pm.groupby(['month', 'source'])['bibliographic.primaryID']
                .nunique()
                .reset_index()
                .rename(columns={'bibliographic.primaryID': 'Items'})
        )
    else:
        # group by month + operator
        output_line = (
            df_pm.groupby(['month', 'digitizer.operator.lastName'])['bibliographic.primaryID']
                .nunique()
                .reset_index()
                .rename(columns={'bibliographic.primaryID': 'Items'})
        )

    output_line['month'] = pd.to_datetime(output_line['month'])
    output_line = output_line.sort_values('month')

    # Now for the lineplot:
    sns.set_style("whitegrid")
    plt.figure(figsize=(12, 6) if not args.historical else (18, 6))
    sns.lineplot(
        data=output_line,
        x='month',
        y='Items',
        hue=hue_column,
        marker='o',
        linewidth=2
    )

    title = f'Monthly Digitization Output (PM role only) - {year_label}'
    plt.title(title)
    plt.xlabel('')
    plt.ylabel('Items Digitized')
    if args.historical:
        plt.xticks(rotation=90)
    else:
        plt.xticks(rotation=45)
    plt.tight_layout()
    plt.legend(title=legend_title)
    plt.show()

    return output_line, year_label, total_items_per_month_summed, total_file_size, total_media_counts, equipment_usage, spec_collection_usage, formatted_grand_total_duration, total_media_counts_by_division, total_counts_by_project_type, output_by_operator, data_by_role

def plot_object_format_counts(df, args, fiscal=False, previous_fiscal=False, top_n=10, formatted_file_size="", total_items_per_month_summed=0, media_counts=None, formatted_grand_total_duration=""):
    if 'digitizer.operator.lastName' not in df.columns:
        print("\nThe 'digitizer.operator.lastName' field is not present in the DataFrame. Skipping the function.\n")
        # Return an empty DataFrame with the columns we expect for plotting
        return pd.DataFrame(columns=['Format', 'Count'])

    # 2) Just create the pm subset
    df_pm = df[df['asset.fileRole'] == 'pm']

    # 3) Derive the chart title from the actual flags
    current_date = datetime.datetime.now()
    current_fiscal_year = get_fiscal_year(current_date)
    prior_fiscal_year = f"FY{int(current_fiscal_year[2:]) - 1}"

    if args.historical:
        year_label = "All Years"
    elif args.previous_fiscal == 'auto':
        year_label = prior_fiscal_year
    elif args.previous_fiscal:  # e.g. "FY23", "FY22"
        year_label = args.previous_fiscal
    elif fiscal:
        year_label = current_fiscal_year
    else:
        year_label = str(current_date.year)

    if args.engineer:
        df_pm = df_pm[df_pm['digitizer.operator.lastName'].isin(args.engineer)]

    format_counts = df_pm.groupby('source.object.format')['bibliographic.primaryID'].nunique().nlargest(top_n).reset_index()
    format_counts.columns = ['Format', 'Count']

    # Plotting with annotations
    fig, ax = plt.subplots(figsize=(15, 6))
    sns.barplot(x='Format', y='Count', data=format_counts, palette='viridis', hue='Format', ax=ax, legend=False)
    plt.xticks(rotation=45)
    plt.xlabel('Format')
    plt.ylabel('Count')
    plt.title(f'Top {top_n} Counts of AMI Digitized in {year_label}', fontsize=16, fontweight='bold')
    plt.subplots_adjust(bottom=0.3)

    # Adding annotations
    for p in ax.patches:
        ax.annotate(f'{int(p.get_height())}', (p.get_x() + p.get_width() / 2., p.get_height()),
                    ha='center', va='bottom', color='black', xytext=(0, 5), textcoords='offset points')

    media_text = ""
    for index, row in media_counts.iterrows():
        media_text += f"\n{row['media_type'].title()}: {row['Total Items Per Media']}"

    # Append formatted_grand_total_duration to the media text
    media_text += f"\nTotal Duration of Digitized Items (HH:MM:SS): {formatted_grand_total_duration}"

    # Display total count of all objects digitized and media type counts
    plt.text(0.95, 0.95, f"Total Items Digitized: {total_items_per_month_summed}\nTotal Data Generated: {formatted_file_size}{media_text}", transform=ax.transAxes, horizontalalignment='right',
            verticalalignment='top', fontsize=14, color='black', bbox=dict(facecolor='white', alpha=0.5))

    plt.show()

    return format_counts

def plot_objects_by_division_code(df, year_label, min_percentage=1):
    df_pm = df[df['asset.fileRole'] == 'pm']
    df_pm = classify_media_types(df_pm)  # Ensure media types are classified

    # Group by media type, month, and division code, then count unique IDs
    objects_by_division_code = df_pm.groupby(['media_type', 'month', 'bibliographic.vernacularDivisionCode']).agg({
        'bibliographic.primaryID': 'nunique'
    }).reset_index()

    # Define the combinations as a dictionary
    combine_dict = {
        'MUS + RHA': ['MUS', 'RHA', 'mym', 'myh'],
        'SCH': ['SCM', 'SCL', 'scb', 'scd'],
        'THE + TOFT': ['THE', 'TOFT', 'myt'],
        'DAN': ['DAN', 'myd'],
        'MSS': ['MSS', 'mao']
    }

    # Iterate over the dictionary to combine the codes
    for new_code, old_codes in combine_dict.items():
        combined_count = objects_by_division_code.loc[objects_by_division_code['bibliographic.vernacularDivisionCode'].isin(old_codes), 'bibliographic.primaryID'].sum()
        # Remove the old codes rows
        objects_by_division_code = objects_by_division_code.loc[~objects_by_division_code['bibliographic.vernacularDivisionCode'].isin(old_codes)]
        # Add a new row for the combined codes
        new_row = pd.DataFrame({
            'bibliographic.vernacularDivisionCode': [new_code],
            'bibliographic.primaryID': [combined_count]
        })
        objects_by_division_code = pd.concat([objects_by_division_code, new_row], ignore_index=True)

    # Calculate the total and percentage
    total_objects = objects_by_division_code['bibliographic.primaryID'].sum()
    objects_by_division_code['Percentage'] = (objects_by_division_code['bibliographic.primaryID'] / total_objects) * 100

    # Filter based on minimum percentage and handle 'Others'
    filtered_data = objects_by_division_code[objects_by_division_code['Percentage'] >= min_percentage]
    others_count = total_objects - filtered_data['bibliographic.primaryID'].sum()
    if others_count > 0:
        others_row = pd.DataFrame({
            'bibliographic.vernacularDivisionCode': ['Others'],
            'bibliographic.primaryID': [others_count]
        })
        filtered_data = pd.concat([filtered_data, others_row], ignore_index=True)

    # Plotting
    colors = sns.cubehelix_palette(start=1.5, rot=-1, dark=0.3, light=0.8, reverse=False, n_colors=len(filtered_data))
    plt.figure(figsize=(10, 10))
    plt.pie(filtered_data['bibliographic.primaryID'], labels=filtered_data['bibliographic.vernacularDivisionCode'], autopct='%1.1f%%', startangle=90, colors=colors)
    plt.axis('equal')
    # Add text annotations for actual counts
    text_str = '\n'.join([f"{row['bibliographic.vernacularDivisionCode']}: {row['bibliographic.primaryID']}" for index, row in filtered_data.iterrows()])
    plt.text(-1.3, -1.3, text_str, fontsize=12, verticalalignment='bottom', 
            bbox=dict(facecolor='white', alpha=0.5, edgecolor='gray', boxstyle='round,pad=1'))
    plt.title(f'Objects Digitized by Division Code in {year_label}', fontsize=16)
    plt.show()

    return filtered_data

def save_plot_to_pdf(
    line_data,
    bar_data,
    filtered_data,
    args,
    total_items_per_month_summed,
    formatted_file_size,
    year_label,
    media_counts,
    equipment_usage,
    spec_collection_usage,
    formatted_grand_total_duration,
    total_media_counts_by_division,
    total_counts_by_project_type,
    output_by_operator
):
    engineer_name = "_".join(args.engineer) if args.engineer else ""
    
    if args.historical:
        report_type = "Historical"
    elif args.fiscal:
        report_type = "Fiscal_Year"
    elif args.previous_fiscal:
        report_type = "Previous_Fiscal_Year"
    else:
        report_type = "Calendar_Year"

    # Decide the PDF filename
    if args.combined:
        pdf_filename = f"Combined_AMI_Digitization_Report_{year_label}.pdf"
    elif args.vendor:
        pdf_filename = f"Vendor_AMI_Digitization_Report_{year_label}.pdf"
    else:
        if engineer_name:
            pdf_filename = f"Digitization_Report_{engineer_name}_{report_type}_{year_label}.pdf"
        else:
            pdf_filename = f"Digitization_Report_{report_type}_{year_label}.pdf"

    pdf_path = os.path.join(os.path.expanduser("~"), 'Desktop', pdf_filename)

    with PdfPages(pdf_path) as pdf:
        fig_size = (18, 6) if args.historical else (10, 5)
        xticks_rotation = 90 if args.historical else 45

        # --- 1) LINE CHART from line_data
        if args.combined or args.vendor:
            hue_column = 'source'
            legend_title = 'Source'
        else:
            hue_column = 'digitizer.operator.lastName'
            legend_title = 'Digitizer'

        fig, ax = plt.subplots(figsize=fig_size)
        sns.lineplot(
            data=line_data,
            x='month',
            y='Items',
            hue=hue_column,
            marker='o',
            linewidth=2,
            ax=ax
        )
        plt.title(f'Monthly Digitization Output - {year_label}')
        plt.xlabel('')
        plt.ylabel('Items Digitized')
        plt.xticks(rotation=xticks_rotation)
        plt.legend(title=legend_title)
        plt.tight_layout()
        pdf.savefig(fig)
        plt.close(fig)

        # --- 2) SUMMARY TABLE from output_by_operator
        if args.historical:
            years = output_by_operator['month'].dt.year.unique()
            for year in sorted(years):
                year_data = output_by_operator[output_by_operator['month'].dt.year == year]
                summary_df = year_data.groupby('month').agg({'bibliographic.primaryID': 'sum'}).reset_index()
                summary_df.columns = ['Month', 'Total Items Digitized']
                summary_df['Month'] = summary_df['Month'].dt.strftime('%Y-%m')
                yearly_total = pd.DataFrame([{
                    'Month': 'Year Total',
                    'Total Items Digitized': summary_df['Total Items Digitized'].sum()
                }])
                summary_df = pd.concat([summary_df, yearly_total], ignore_index=True)

                fig, ax = plt.subplots(figsize=(12, len(summary_df) * 0.5))
                ax.axis('off')
                table = ax.table(
                    cellText=summary_df.values,
                    colLabels=summary_df.columns,
                    loc='center',
                    cellLoc='center'
                )
                table.auto_set_font_size(True)
                table.scale(1.2, 1.5)
                pdf.savefig(fig)
                plt.close(fig)
        else:
            summary_df = output_by_operator.groupby('month').agg({'bibliographic.primaryID': 'sum'}).reset_index()
            summary_df.columns = ['Month', 'Total Items Digitized']
            summary_df['Month'] = summary_df['Month'].dt.strftime('%Y-%m')
            yearly_total = pd.DataFrame([{
                'Month': 'Year Total',
                'Total Items Digitized': summary_df['Total Items Digitized'].sum()
            }])
            summary_df = pd.concat([summary_df, yearly_total], ignore_index=True)

            fig, ax = plt.subplots(figsize=(10, 5))
            ax.axis('off')
            table = ax.table(
                cellText=summary_df.values,
                colLabels=summary_df.columns,
                loc='center',
                cellLoc='center'
            )
            table.auto_set_font_size(True)
            table.scale(1.2, 1.2)
            pdf.savefig(fig)
            plt.close(fig)
  
        media_text = ""
        for index, row in media_counts.iterrows():
            media_text += f"\n{row['media_type'].title()}: {row['Total Items Per Media']}"
        
        # Append formatted_grand_total_duration to the media text
        media_text += f"\nTotal Duration of Digitized Items (HH:MM:SS): {formatted_grand_total_duration}"
        
        # Bar plot with annotations for object format counts
        fig, ax = plt.subplots(figsize=(12, 6))
        sns.barplot(x='Format', y='Count', data=bar_data, palette='viridis', hue='Format', ax=ax, legend=False)
        plt.xticks(rotation=45)
        plt.xlabel('Format')
        plt.ylabel('Count')
        plt.title(f'Top {len(bar_data)} Counts of AMI Digitized in {year_label}', fontsize=16)
        plt.subplots_adjust(bottom=0.3)
        for p in ax.patches:
            ax.annotate(f'{int(p.get_height())}', (p.get_x() + p.get_width() / 2., p.get_height()),
                        ha='center', va='bottom', color='black', xytext=(0, 5), textcoords='offset points')
        
        # Display total count of all objects digitized, not just those in the chart
        plt.text(0.95, 0.95, f"Total Items Digitized: {total_items_per_month_summed}\nTotal Data Generated: {formatted_file_size}{media_text}", transform=ax.transAxes, horizontalalignment='right',
                verticalalignment='top', fontsize=14, color='black', bbox=dict(facecolor='white', alpha=0.5))
        plt.tight_layout()
        pdf.savefig(fig)
        plt.close(fig)

        # Pie chart for division codes
        colors = sns.cubehelix_palette(start=1.5, rot=-1, dark=0.3, light=0.8, reverse=False, n_colors=len(filtered_data))
        fig, ax = plt.subplots(figsize=(10, 10))
        ax.pie(filtered_data['bibliographic.primaryID'], labels=filtered_data['bibliographic.vernacularDivisionCode'], autopct='%1.1f%%', startangle=90, colors=colors)
        ax.set_title(f'Objects Digitized by Division Code in {year_label}', fontsize=16)
        ax.axis('equal')  
        # Add text annotations for actual counts
        text_str = '\n'.join([f"{row['bibliographic.vernacularDivisionCode']}: {row['bibliographic.primaryID']}" for index, row in filtered_data.iterrows()])
        plt.text(-1.3, -1.3, text_str, fontsize=12, verticalalignment='bottom', 
                bbox=dict(facecolor='white', alpha=0.5, edgecolor='gray', boxstyle='round,pad=1'))
        pdf.savefig(fig)
        plt.close(fig)

        # Generate pie charts for each media type
        media_types = total_media_counts_by_division['media_type'].unique()
        fig, axes = plt.subplots(nrows=1, ncols=len(media_types), figsize=(5 * len(media_types), 6.5))
        if len(media_types) == 1:
            axes = [axes]  # Ensure axes is iterable for a single subplot

        for ax, media_type in zip(axes, media_types):
            # Filter data for the current media type
            data = total_media_counts_by_division[total_media_counts_by_division['media_type'] == media_type]
            
            # Generate a pie chart
            colors = sns.color_palette("cubehelix", len(data))  
            wedges, _ = ax.pie(data['Unique Items'], startangle=90, colors=colors)
            ax.set_title(f'{media_type.title()} by Division', fontsize=16)
            ax.axis('equal')  # Equal aspect ratio ensures that pie is drawn as a circle.
            
            # Use a legend instead of pie labels to prevent overlapping text for small slices
            labels = [f"{div} ({pct})" for div, pct in zip(data['bibliographic.vernacularDivisionCode'], data['Percentage'])]
            ax.legend(wedges, labels, loc="upper center", bbox_to_anchor=(0.5, 0.0), fontsize=10, frameon=False)

        plt.tight_layout()
        # Adjust bottom to make room for the legend
        fig.subplots_adjust(bottom=0.25)
        pdf.savefig(fig)  # Save the full figure with all pie charts
        plt.close(fig)

        fig, ax = plt.subplots(figsize=(12, 6))
        # Sort by descending count if desired
        total_counts_by_project_type = total_counts_by_project_type.sort_values('Unique Items', ascending=False)

        sns.barplot(
            x='Unique Items',
            y='projectType',
            data=total_counts_by_project_type,
            hue='projectType',           # assign hue for multicolor mapping
            palette='Paired',
            ax=ax,
            dodge=False
        )

        # Remove the legend since each bar is uniquely colored.
        if ax.get_legend() is not None:
            ax.get_legend().remove()

        ax.set_title(f'Objects Digitized by Project Type ({year_label})', fontsize=16)
        ax.set_xlabel('Number of Unique Items')
        ax.set_ylabel('Project Type')

        # Annotate each bar
        for p in ax.patches:
            width = p.get_width()
            ax.annotate(f'{int(width)}',
                        xy=(width, p.get_y() + p.get_height() / 2),
                        xytext=(5, 0),  # offset
                        textcoords='offset points',
                        ha='left', va='center')

        plt.tight_layout()
        pdf.savefig(fig)
        plt.close(fig)


        # Items Digitized Per SPEC Collection ID
        # Cutting out text after comma characters for collection titles
        spec_collection_usage['SPEC Collection Title'] = spec_collection_usage['SPEC Collection Title'].str.split(',').str[0]

        top_collections = spec_collection_usage.sort_values(by='Unique Items', ascending=False).head(15)  # Display top 15 collections
        fig, ax = plt.subplots(figsize=(12, 8))

        # Generate a cubehelix palette
        num_colors = len(top_collections)
        palette = sns.cubehelix_palette(start=2, rot=0, dark=0.3, light=0.8, n_colors=num_colors, reverse=True)

        # Plotting with the custom palette
        bars = sns.barplot(x='Unique Items', y='SPEC Collection Title', data=top_collections, palette=palette, hue='SPEC Collection Title', ax=ax, legend=False)
        ax.set_yticklabels([])  # Hide y-axis labels

        # Calculate the median or mean width of the bars to use as a threshold
        bar_widths = [bar.get_width() for bar in bars.patches]
        adaptive_threshold = np.mean(bar_widths)  # You can use np.mean(bar_widths) if preferred

        # Annotate each bar with the corresponding SPEC Collection Title
        for bar, title in zip(bars.patches, top_collections['SPEC Collection Title']):
            bar_width = bar.get_width()  # Get the width of the bar
            if bar_width > adaptive_threshold:  # Use the adaptive threshold
                text_x = bar_width - 10
                va_position = 'center'
                ha_position = 'right'
                color = 'white'  # Better readability for text inside bars
            else:
                text_x = bar_width + 10  # Position outside the bar
                va_position = 'center'
                ha_position = 'left'
                color = 'black'
            
            ax.text(text_x, bar.get_y() + bar.get_height()/2, title, va=va_position, ha=ha_position, color=color, fontweight='bold')

        plt.title('Top SPEC Collections Digitized', fontsize=16)
        plt.xlabel('Number of Unique Items')
        plt.ylabel('')  # Remove the y-axis label
        plt.tight_layout()
        pdf.savefig(fig)
        plt.close(fig)

        if equipment_usage.empty:
            print("No equipment usage data to plot.")
        else:
            # Generate a palette that matches the number of items being displayed
            num_display = 15  # Number of items you are displaying
            palette = sns.cubehelix_palette(n_colors=num_display, start=1.5, rot=-0.5, dark=0.3, light=0.8, reverse=True)

            fig, ax = plt.subplots(figsize=(12, 8))
            # Apply the palette by using the hue parameter
            # Assuming 'Label' is what you're differentiating by color
            sns.barplot(x='Unique Items', y='Label', data=equipment_usage.head(num_display), palette=palette, hue='Label', ax=ax, legend=False)
            plt.title('Top Equipment Used (In-House)', fontsize=16)
            plt.xlabel('Number of Unique Items Processed')
            plt.ylabel('Equipment Model and Serial Number')
            plt.tight_layout()
            pdf.savefig(fig)
            plt.close(fig)

    print(f"PDF report has been saved to {pdf_path}.")

def main():
    args = get_args()
    #If an input CSV is provided, use it; otherwise, fetch data via JDBC.
    if args.input_csv:
        print(f"Using input CSV: {args.input_csv}")
        df = pd.read_csv(args.input_csv, low_memory=False)
        df['technical.durationMilli.measure'] = pd.to_numeric(df['technical.durationMilli.measure'], errors='coerce')
    else:
        df = fetch_data_from_jdbc(args)
        if not df.empty and args.output_csv:
            df.to_csv(args.output_csv, index=False)
            print(f"CSV output written to: {args.output_csv}")
    
    if df.empty:
        print("No data retrieved from the FileMaker database. Please check your network connection or host settings.")
        return
    
    df_processed = process_data(df, args, fiscal=args.fiscal)
    line_data, year_label, total_items_per_month_summed, total_file_size, media_counts, equipment_usage, spec_collection_usage, formatted_grand_total_duration, total_media_counts_by_division, total_counts_by_project_type, output_by_operator, data_by_role = display_monthly_output(df_processed, args, fiscal=args.fiscal)
    if line_data is None:
        print("Error: Missing data. Exiting the program.")
        return
    
    formatted_file_size = format_file_size(total_file_size)

    filtered_data = plot_objects_by_division_code(df_processed, year_label)

    bar_data = plot_object_format_counts(df_processed, args, fiscal=args.fiscal, previous_fiscal=args.previous_fiscal, formatted_file_size=formatted_file_size, total_items_per_month_summed=total_items_per_month_summed, media_counts=media_counts, formatted_grand_total_duration=formatted_grand_total_duration)
    save_plot_to_pdf(line_data, bar_data, filtered_data, args, total_items_per_month_summed, formatted_file_size, year_label, media_counts, equipment_usage, spec_collection_usage, formatted_grand_total_duration, total_media_counts_by_division, total_counts_by_project_type, output_by_operator)

if __name__ == "__main__":
    main()
