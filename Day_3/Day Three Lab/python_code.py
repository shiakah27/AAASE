# ======================================================
# ======================================================
# SAVE REPORT FUNCTION
# ======================================================


def save_report(report_data):

    filename = "final_report.txt"

    with open(filename, "w", encoding="utf-8") as file:

        file.write("AI GENERATED REPORT\n")
        file.write("=" * 60 + "\n\n")

        file.write(f"Topic: {report_data['topic']}\n\n")

        file.write("FINAL REPORT\n")
        file.write("-" * 60 + "\n\n")

        file.write(report_data['final_report'])

    print(f"\nReport saved successfully: {filename}")


# ======================================================
# OPTIONAL: STORE RESULTS IN DATAFRAME
# ======================================================


def create_dataframe(report_data):

    data = {
        "Topic": [report_data['topic']],
        "Generated_Date": [datetime.now()],
        "Report_Length": [len(report_data['final_report'])]
    }

    df = pd.DataFrame(data)

    print("\nData Summary")
    print(df)

    return df


# ======================================================
# MAIN EXECUTION
# ======================================================

if __name__ == "__main__":

    manager = ReportManager()

    report_data = manager.generate_report(TOPIC)

    save_report(report_data)

    df = create_dataframe(report_data)

    print("\nSystem Finished Successfully")


# ======================================================
# END OF PROJECT
# ======================================================