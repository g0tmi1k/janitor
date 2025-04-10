use janitor::publish::MergeProposalStatus;
use std::collections::HashMap;

#[derive(Debug)]
pub enum RateLimitStatus {
    Allowed,
    RateLimited,
    BucketRateLimited {
        bucket: String,
        open_mps: usize,
        max_open_mps: usize,
    },
}

impl RateLimitStatus {
    pub fn is_allowed(&self) -> bool {
        match self {
            RateLimitStatus::Allowed => true,
            _ => false,
        }
    }
}

impl std::fmt::Display for RateLimitStatus {
    fn fmt(&self, f: &mut std::fmt::Formatter) -> std::fmt::Result {
        match self {
            RateLimitStatus::Allowed => write!(f, "Allowed"),
            RateLimitStatus::RateLimited => write!(f, "RateLimited"),
            RateLimitStatus::BucketRateLimited {
                bucket,
                open_mps,
                max_open_mps,
            } => write!(
                f,
                "BucketRateLimited: bucket={}, open_mps={}, max_open_mps={}",
                bucket, open_mps, max_open_mps
            ),
        }
    }
}

pub struct RateLimitStats {
    pub per_bucket: HashMap<String, usize>,
}

pub trait RateLimiter: Send + Sync {
    fn set_mps_per_bucket(
        &mut self,
        mps_per_bucket: &HashMap<MergeProposalStatus, HashMap<String, usize>>,
    );

    fn check_allowed(&self, bucket: &str) -> RateLimitStatus;

    fn inc(&mut self, bucket: &str);

    fn get_stats(&self) -> Option<RateLimitStats>;

    fn get_max_open(&self, bucket: &str) -> Option<usize> {
        None
    }
}

pub struct NonRateLimiter;

impl NonRateLimiter {
    pub fn new() -> Self {
        NonRateLimiter
    }
}

impl RateLimiter for NonRateLimiter {
    fn set_mps_per_bucket(
        &mut self,
        _mps_per_bucket: &HashMap<MergeProposalStatus, HashMap<String, usize>>,
    ) {
    }

    fn check_allowed(&self, _bucket: &str) -> RateLimitStatus {
        RateLimitStatus::Allowed
    }

    fn inc(&mut self, _bucket: &str) {}

    fn get_stats(&self) -> Option<RateLimitStats> {
        None
    }
}

pub struct FixedRateLimiter {
    max_mps_per_bucket: usize,
    open_mps_per_bucket: Option<HashMap<String, usize>>,
}

impl FixedRateLimiter {
    pub fn new(max_mps_per_bucket: usize) -> Self {
        FixedRateLimiter {
            max_mps_per_bucket,
            open_mps_per_bucket: None,
        }
    }
}

impl RateLimiter for FixedRateLimiter {
    fn set_mps_per_bucket(
        &mut self,
        mps_per_bucket: &HashMap<MergeProposalStatus, HashMap<String, usize>>,
    ) {
        self.open_mps_per_bucket = mps_per_bucket.get(&MergeProposalStatus::Open).cloned();
    }

    fn check_allowed(&self, bucket: &str) -> RateLimitStatus {
        if let Some(open_mps_per_bucket) = &self.open_mps_per_bucket {
            if let Some(current) = open_mps_per_bucket.get(bucket) {
                if *current > self.max_mps_per_bucket {
                    return RateLimitStatus::BucketRateLimited {
                        bucket: bucket.to_string(),
                        open_mps: *current,
                        max_open_mps: self.max_mps_per_bucket,
                    };
                }
            }
        } else {
            // Be conservative
            return RateLimitStatus::RateLimited;
        }
        RateLimitStatus::Allowed
    }

    fn inc(&mut self, bucket: &str) {
        if let Some(open_mps_per_bucket) = self.open_mps_per_bucket.as_mut() {
            open_mps_per_bucket
                .entry(bucket.to_string())
                .and_modify(|e| *e += 1)
                .or_insert(1);
        }
    }

    fn get_stats(&self) -> Option<RateLimitStats> {
        self.open_mps_per_bucket
            .as_ref()
            .map(|open_mps_per_bucket| RateLimitStats {
                per_bucket: open_mps_per_bucket.clone(),
            })
    }
}

pub struct SlowStartRateLimiter {
    max_mps_per_bucket: Option<usize>,
    open_mps_per_bucket: Option<HashMap<String, usize>>,
    absorbed_mps_per_bucket: Option<HashMap<String, usize>>,
}

impl SlowStartRateLimiter {
    pub fn new(max_mps_per_bucket: Option<usize>) -> Self {
        SlowStartRateLimiter {
            max_mps_per_bucket,
            open_mps_per_bucket: None,
            absorbed_mps_per_bucket: None,
        }
    }

    fn get_limit(&self, bucket: &str) -> Option<usize> {
        if let Some(absorbed_mps_per_bucket) = &self.absorbed_mps_per_bucket {
            absorbed_mps_per_bucket.get(bucket).map(|c| c + 1)
        } else {
            None
        }
    }
}

impl RateLimiter for SlowStartRateLimiter {
    fn check_allowed(&self, bucket: &str) -> RateLimitStatus {
        if let Some(max_mps_per_bucket) = self.max_mps_per_bucket {
            if let Some(open_mps_per_bucket) = &self.open_mps_per_bucket {
                if let Some(current) = open_mps_per_bucket.get(bucket) {
                    if *current > max_mps_per_bucket {
                        return RateLimitStatus::BucketRateLimited {
                            bucket: bucket.to_string(),
                            open_mps: *current,
                            max_open_mps: max_mps_per_bucket,
                        };
                    }
                }
            } else {
                // Be conservative
                return RateLimitStatus::RateLimited;
            }
        } else {
            // Be conservative
            return RateLimitStatus::RateLimited;
        }
        RateLimitStatus::Allowed
    }

    fn inc(&mut self, bucket: &str) {
        if let Some(open_mps_per_bucket) = self.open_mps_per_bucket.as_mut() {
            open_mps_per_bucket
                .entry(bucket.to_string())
                .and_modify(|e| *e += 1)
                .or_insert(1);
        }
    }

    fn set_mps_per_bucket(
        &mut self,
        mps_per_bucket: &HashMap<MergeProposalStatus, HashMap<String, usize>>,
    ) {
        self.open_mps_per_bucket = mps_per_bucket.get(&MergeProposalStatus::Open).cloned();
        let mut absorbed_mps_per_bucket = HashMap::new();
        for status in [MergeProposalStatus::Merged, MergeProposalStatus::Applied] {
            for (bucket, count) in mps_per_bucket.get(&status).unwrap_or(&HashMap::new()) {
                absorbed_mps_per_bucket
                    .entry(bucket.to_string())
                    .and_modify(|e| *e += count)
                    .or_insert(*count);
            }
        }
        self.absorbed_mps_per_bucket = Some(absorbed_mps_per_bucket);
    }

    fn get_stats(&self) -> Option<RateLimitStats> {
        if let Some(open_mps_per_bucket) = &self.open_mps_per_bucket {
            Some(RateLimitStats {
                per_bucket: open_mps_per_bucket
                    .iter()
                    .map(|(k, _v)| {
                        (
                            k.clone(),
                            std::cmp::min(
                                self.max_mps_per_bucket.unwrap(),
                                self.get_limit(k).unwrap(),
                            ),
                        )
                    })
                    .collect(),
            })
        } else {
            None
        }
    }
}
