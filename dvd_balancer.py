import streamlit as st
import pandas as pd
import io

# --- 1. HELPER FUNCTIONS ---
def get_collection_group(loc):
    """Categorizes by suffix for like-to-like matching."""
    suffix = str(loc)[-2:].lower()
    if suffix in ['jv', 'js']: return "Juvenile"
    if suffix == 'vd': return "Adult"
    return "General/Other"

def to_excel_tabs(df, group_col):
    """Helper to split a dataframe into an Excel file with multiple tabs."""
    output = io.BytesIO()
    with pd.ExcelWriter(output, engine='openpyxl') as writer:
        # Sort by the location/group column
        groups = sorted(df[group_col].unique())
        for group in groups:
            temp_df = df[df[group_col] == group]
            # Excel sheet names limited to 31 chars
            sheet_name = str(group)[:31]
            temp_df.to_excel(writer, index=False, sheet_name=sheet_name)
    return output.getvalue()

# --- 2. APP UI SETUP ---
st.set_page_config(page_title="Library DVD Balancer", layout="wide")
st.title("DVD Collection Balancer")

st.sidebar.header("Global Settings")
target_fill = st.sidebar.slider("Target Fill Ratio", 0.5, 1.0, 0.85)
min_move = st.sidebar.number_input("Minimum Move Threshold", min_value=1, value=25)
dvds_per_inch = float(st.sidebar.number_input("DVDs per Linear Inch", value=1.8))

col_u1, col_u2 = st.columns(2)
with col_u1:
    collection_file = st.file_uploader("1. Upload DVD Collection CSV", type=["csv"])
with col_u2:
    shelf_file = st.file_uploader("2. Upload Shelf Lengths CSV", type=["csv"])

if collection_file and shelf_file:
    # --- LOAD COLLECTION DATA ---
    df = pd.read_csv(collection_file)
    df['LOCATION'] = df['LOCATION'].str.strip()
    df['STATUS'] = df['STATUS'].str.strip()
    df['LCHKIN'] = pd.to_datetime(df['LCHKIN'], errors='coerce')
    
    # --- LOAD AND CLEAN SHELF DATA ---
    shelf_df = pd.read_csv(shelf_file)
    shelf_df.columns = [c.strip().lower() for c in shelf_df.columns]
    
    if 'location' not in shelf_df.columns or 'inches' not in shelf_df.columns:
        st.error("Shelf CSV must have 'Location' and 'Inches' columns.")
        st.stop()
    
    shelf_df['inches'] = pd.to_numeric(shelf_df['inches'], errors='coerce').fillna(0)
    shelf_lookup = shelf_df.groupby('location')['inches'].sum().to_dict()

    st.header("1. Verify Loaded Capacities")
    branches = sorted(df['LOCATION'].unique())
    cap_cols = st.columns(4)
    capacities_inches = {}

    for i, branch in enumerate(branches):
        with cap_cols[i % 4]:
            default_val = float(shelf_lookup.get(branch, 0.0))
            capacities_inches[branch] = st.number_input(f"Inches at {branch}", value=default_val)

    # --- CALCULATION ---
    if st.button("Generate Balanced Plan"):
        capacities = {loc: int(float(inch) * float(dvds_per_inch)) for loc, inch in capacities_inches.items()}
        df_on_shelf = df[df['STATUS'] == '-'].copy()
        full_counts = df['LOCATION'].value_counts().to_dict()
        
        stats_list = []
        for loc, cap in capacities.items():
            if cap <= 0: continue
            curr = full_counts.get(loc, 0)
            target = int(cap * target_fill)
            delta = curr - target
            stats_list.append({
                'Branch': loc, 'Group': get_collection_group(loc),
                'Current': curr, 'Capacity': cap, 'Surplus': delta
            })
        
        stats_df = pd.DataFrame(stats_list)
        if stats_df.empty:
            st.warning("No branches with capacity found.")
            st.stop()

        # --- REDISTRIBUTION ENGINE ---
        move_list, weed_list = [], []
        
        for group_name in stats_df['Group'].unique():
            group_stats = stats_df[stats_df['Group'] == group_name].copy()
            group_givers = group_stats[group_stats['Surplus'] >= min_move].copy()
            group_receivers = group_stats[group_stats['Surplus'] < 0].copy()
            
            total_surplus = group_givers['Surplus'].sum()
            total_room = abs(group_receivers['Surplus'].sum())
            
            if total_surplus > total_room:
                num_to_weed = int(total_surplus - total_room)
                potential_weeds = df_on_shelf[df_on_shelf['LOCATION'].isin(group_givers['Branch'])]
                potential_weeds = potential_weeds.sort_values(by=['TOT CHKOUT', 'LCHKIN'], ascending=[True, True])
                
                group_weeds = potential_weeds.head(num_to_weed).copy()
                weed_list.append(group_weeds)
                df_on_shelf = df_on_shelf[~df_on_shelf['BARCODE'].isin(group_weeds['BARCODE'])]
                
                for loc, count in group_weeds['LOCATION'].value_counts().items():
                    group_givers.loc[group_givers['Branch'] == loc, 'Surplus'] -= count

            for _, giver in group_givers.iterrows():
                num_to_move = int(giver['Surplus'])
                if num_to_move <= 0: continue
                branch_items = df_on_shelf[df_on_shelf['LOCATION'] == giver['Branch']].sort_values('LCHKIN')
                items = branch_items.head(num_to_move)
                
                for _, item in items.iterrows():
                    if group_receivers.empty: break
                    rec_idx = group_receivers.index[0]
                    move_list.append({
                        'Barcode': item['BARCODE'], 'Title': item['245|abpn'],
                        'From': giver['Branch'], 'To': group_receivers.loc[rec_idx, 'Branch'],
                        'Group': group_name
                    })
                    group_receivers.loc[rec_idx, 'Surplus'] += 1
                    if group_receivers.loc[rec_idx, 'Surplus'] >= 0:
                        group_receivers = group_receivers.drop(rec_idx)

        # --- DISPLAY RESULTS ---
        st.header("2. Results Summary")
        final_moves = pd.DataFrame(move_list)
        final_weeds = pd.concat(weed_list) if weed_list else pd.DataFrame()

        c1, c2, c3 = st.columns(3)
        c1.metric("Items to Move", len(final_moves))
        c2.metric("Items to Weed", len(final_weeds))
        
        total_items, total_cap = stats_df['Current'].sum(), stats_df['Capacity'].sum()
        fill_pct = int((total_items / total_cap) * 100) if total_cap > 0 else 0
        c3.metric("System Fill", f"{fill_pct}%")

        st.subheader("Branch Balance Report")
        st.dataframe(stats_df, use_container_width=True)

        st.subheader("3. Download Lists (Multi-Tab Excel)")
        d1, d2 = st.columns(2)
        
        if not final_moves.empty:
            excel_moves = to_excel_tabs(final_moves, 'From')
            d1.download_button(
                label="Download Pull List by Branch",
                data=excel_moves,
                file_name="DVD_Pull_Lists_by_Branch.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            )
            
        if not final_weeds.empty:
            excel_weeds = to_excel_tabs(final_weeds, 'LOCATION')
            d2.download_button(
                label="Download Weed List by Branch",
                data=excel_weeds,
                file_name="DVD_Weed_Lists_by_Branch.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            )
else:
    st.info("Please upload both CSV files to begin.")