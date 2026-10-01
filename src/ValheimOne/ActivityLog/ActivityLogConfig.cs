using System;
using ValheimOne.Configuration;

namespace ValheimOne.ActivityLog;

internal sealed class ActivityLogConfig
{
    private readonly ConfigEntryInt _retentionDays;
    private readonly ConfigEntryInt _feedRetentionDays;

    public ActivityLogConfig(ConfigEntryInt retentionDays, ConfigEntryInt feedRetentionDays)
    {
        _retentionDays = retentionDays;
        _feedRetentionDays = feedRetentionDays;
    }

    public int RetentionDays => Math.Max(1, Math.Min(3650, _retentionDays.Value));

    public int FeedRetentionDays => Math.Max(1, Math.Min(3650, _feedRetentionDays.Value));
}
