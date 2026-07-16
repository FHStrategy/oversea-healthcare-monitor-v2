let currentView = 'list';
        let currentFilter = null;
        let currentTab = 'today';
        
        // 切换视图
        function switchView(view, btn) {
            currentView = view;
            document.querySelectorAll('.view-btn').forEach(el => el.classList.remove('active'));
            btn.classList.add('active');
            
            if (view === 'map') {
                document.getElementById('map-view').classList.add('active');
                document.querySelectorAll('.tab-content').forEach(el => el.style.display = 'none');
                document.querySelector('.tabs').style.display = 'none';
                // 同步地图内的时间选择
                document.querySelectorAll('.map-tab').forEach(el => el.classList.remove('active'));
                document.querySelector('.map-tab[onclick*="' + currentTab + '"]').classList.add('active');
                updateMapData(currentTab);
            } else {
                document.getElementById('map-view').classList.remove('active');
                document.querySelectorAll('.tab-content').forEach(el => {
                    el.style.display = el.classList.contains('active') ? 'block' : 'none';
                });
                document.querySelector('.tabs').style.display = 'flex';
            }
        }
        
        // 更新地图数据
        function updateMapData(tabName) {
            const counts = locationCounts[tabName];
            document.querySelectorAll('.region-card').forEach(card => {
                const location = card.getAttribute('data-location');
                const count = counts[location] || 0;
                card.querySelector('.count-num').textContent = count;
                card.classList.remove('has-news', 'no-news');
                card.classList.add(count > 0 ? 'has-news' : 'no-news');
            });
        }
        
        // 地图内时间选择
        function selectMapTime(tabName, btn) {
            currentTab = tabName;
            document.querySelectorAll('.map-tab').forEach(el => el.classList.remove('active'));
            btn.classList.add('active');
            updateMapData(tabName);
        }
        
        // 按国家筛选
        function filterByLocation(location) {
            currentFilter = location;
            
            // 切换到列表视图
            document.querySelectorAll('.view-btn').forEach(el => el.classList.remove('active'));
            document.querySelector('.view-btn').classList.add('active');
            document.getElementById('map-view').classList.remove('active');
            document.querySelector('.tabs').style.display = 'flex';
            
            // 同步时间选择到列表视图
            document.querySelectorAll('.tab-content').forEach(el => {
                el.classList.remove('active');
                el.style.display = 'none';
            });
            document.querySelectorAll('.tab-btn').forEach(el => el.classList.remove('active'));
            
            // 激活当前时间对应的标签页
            const activeTab = document.getElementById('tab-' + currentTab);
            activeTab.classList.add('active');
            activeTab.style.display = 'block';
            document.querySelector('.tab-btn[onclick*="' + currentTab + '"]').classList.add('active');
            
            // 更新统计和排行榜
            document.getElementById('stat-label').textContent = statsData[currentTab].label;
            document.getElementById('stat-value').textContent = statsData[currentTab].value;
            const rankingHtml = document.getElementById('ranking-' + currentTab + '-data').innerHTML;
            document.getElementById('ranking-container').innerHTML = rankingHtml;
            
            // 显示国家筛选提示
            document.getElementById('country-filter').classList.add('active');
            document.getElementById('filter-name').textContent = location + ' 动态';
            
            // 筛选对应国家的新闻
            document.querySelectorAll('.group-section').forEach(el => {
                const headerText = el.querySelector('.group-header h2').textContent;
                if (headerText.includes(location)) {
                    el.style.display = 'block';
                } else {
                    el.style.display = 'none';
                }
            });
        }
        
        // 清除筛选
        function clearFilter() {
            currentFilter = null;
            document.getElementById('country-filter').classList.remove('active');
            document.querySelectorAll('.group-section').forEach(el => {
                el.style.display = 'block';
            });
        }
        
        function showTab(tabName, btn) {
            currentTab = tabName;
            document.querySelectorAll('.tab-content').forEach(el => {
                el.classList.remove('active');
                el.style.display = 'none';
            });
            document.querySelectorAll('.tab-btn').forEach(el => {
                el.classList.remove('active');
            });
            const activeContent = document.getElementById('tab-' + tabName);
            activeContent.classList.add('active');
            activeContent.style.display = 'block';
            btn.classList.add('active');
            
            document.getElementById('stat-label').textContent = statsData[tabName].label;
            document.getElementById('stat-value').textContent = statsData[tabName].value;
            
            const rankingHtml = document.getElementById('ranking-' + tabName + '-data').innerHTML;
            document.getElementById('ranking-container').innerHTML = rankingHtml;
            
            if (currentFilter) clearFilter();
        }
