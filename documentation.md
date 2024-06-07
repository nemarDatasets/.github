# Forking Openneuro dataset

When forking a dataset on Openneuro, make sure to deselect the option to only fork the main branch, as we also need to get the git-annex branch for datalad to get the large data files.

# Creating S3 bucket for NEMAR

The [tutorial on Datalad handbook](https://handbook.datalad.org/en/latest/basics/101-139-s3.html) (as of June 7, 2024) shows how to create an S3 bucket to host our own data. Note that running the `git annex initremote` command will automatically create the bucket so we don't have to create the bucket ourselves first. As of April 2023, S3 also disables the ability to enable public access at bucket creation, thus we would need to change the `public=yes` to `no`. The modified command would thus be:
```
git annex initremote public-s3 type=S3 encryption=none \
bucket=$BUCKET public=no datacenter=EU autoenable=true
```

Note that `public-s3` is the name we want to assign to our special remote, so we can use any name. In our case for example, we can name it `nemar-s3` instead.

Once the bucket is created you can then go into the **Permissions** setting of the bucket to turn off "Block all public access", and add the policy below to allow public read, which is needed for others to download your data:
```
{
    "Version": "2012-10-17",
    "Statement": [
        {
            "Sid": "PublicRead",
            "Effect": "Allow",
            "Principal": "*",
            "Action": [
                "s3:GetObject",
                "s3:GetObjectVersion"
            ],
            "Resource": "arn:aws:s3:::nemar-dataset/*"
        },
        {
            "Sid": "PublicList",
            "Effect": "Allow",
            "Principal": "*",
            "Action": "s3:ListBucket",
            "Resource": "arn:aws:s3:::nemar-dataset"
        }
    ]
}
```

# Updating dataset

Our dataset is now managed by both Github and S3. Under datalad's eyes, they are referred to by name:
```
$ datalad siblings
.: here(+) [git]
.: public-s3(+) [git]
.: s3-PUBLIC(+) [git]
.: origin(-) [https://github.com/nemarDatasets/ds004350.git (git)]
```
Here the Github endpoint name is `origin` and our S3 bucket is `public-s3`. We will be using these names for other commands when working with these resources.

We would need to also tell datalad to associate the new S3 special remote with our github repo, so that when pushing changes metadata will go to our Github fork and large data files managed by git-annex will go to the S3 bucket. After adding the S3 bucket as special remote following the tutorial above, you will need to set the dependency using the command:
```
$ datalad siblings -d . -s origin --publish-depends public-s3
```
[This section in Datalad handbook](https://handbook.datalad.org/en/latest/basics/101-139-s3.html#publish-the-dataset) use the `datalad create-sibling-github` command. This command would create the Github repo from scratch, but since we already had our fork we use the `datalad siblings -s origin` command to update the repo setting instead.

The dataset can be updated and modified as need to, then use the `datalad save -m "commit message"` command to log our changes with git and git-annex. When all changes are finished, publish your changes  using `datalad push --to origin`.